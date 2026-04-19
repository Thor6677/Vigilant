# Phase 2: Port Plan

**Pyfa reference commit:** `2651316f980fa9e36e638938e6e01eb3fe3c0e2e`

---

## Port Sequence (dependency-ordered)

### 3.1 — Hull resonance attribute fix

**Gap:** Engine reads hull resonances from attrs 974-977 (`hullEmDamageResonance`,
etc.). Ships store hull resonances in attrs 109/110/111/113 (`emDamageResonance`,
`kineticDamageResonance`, etc.). Result: hull resists always 0%.

**Algorithm (plain language):**
Ships use the generic damage resonance attributes (109=kinetic, 110=thermal,
111=explosive, 113=EM) for hull/structure layer. These are the ship's base hull
resonance values (typically 0.67 = 33% resist). Modules like Damage Control modify
these attrs via `ItemModifier` with `domain=shipID`. The hull-specific attrs
(974-977) are used BY hull-tanking modules as their source values, not as the
ship's own hull resonance.

**Changes:**
- `app/fitting/constants.py`: Change `ATTR_HULL_EM_RESONANCE` from 974 to 113,
  `ATTR_HULL_KIN_RESONANCE` from 976 to 109, `ATTR_HULL_THERM_RESONANCE` from
  977 to 110, `ATTR_HULL_EXPL_RESONANCE` from 975 to 111.

**Test:** Drake with DC II should show ~60% hull resists instead of 0%.

**SDE gaps:** None.

---

### 3.2 — OwnerRequiredSkillModifier for ship hull bonuses

**Gap:** Ship hull bonuses for missiles and drones use `OwnerRequiredSkillModifier`
with `domain=charID` to modify charge damage attributes (missiles) and drone
`damageMultiplier`. The engine only processes `LocationGroupModifier` and
`LocationRequiredSkillModifier` with `domain=shipID`. Every missile and drone
ship has broken DPS.

**Algorithm (plain language):**
`OwnerRequiredSkillModifier` targets items "owned by the character" that require a
specific skill. In the context of a fitted ship, this means charges loaded into
weapons (which require weapon skills like Heavy Missiles) and drones (which
require the Drones skill). The modifier reads a bonus attribute from the source
item (the ship) and applies it to matching charges/drones.

For ship hull bonuses, the source attribute is on the ship (e.g., Drake's
`shipBonusCBC1` = 10.0), and the target is a charge damage attribute (e.g.,
`kineticDamage` attr 117 on missiles). The modifier applies as `postPercent`:
`charge_damage *= (1 + bonus_value * skill_level / 100)` for per-level bonuses,
or `charge_damage *= (1 + bonus_value / 100)` for role bonuses.

For drone bonuses (e.g., Gila's 500% `damageMultiplier` bonus), the target is
attr 64 on drones, filtered by skill requirement (e.g., Medium Drone Operation).

**Implementation approach:**
1. In `_apply_ship_hull_bonuses()`, handle `OwnerRequiredSkillModifier`:
   - If the target attr is a charge damage attribute (114/116/117/118), apply
     the bonus to `charge_attrs_map` — a new mutable copy of charge attributes
     that the DPS loop reads from.
   - If the target attr is `damageMultiplier` (64) or other module attr, apply
     to matching items in `module_attrs_map` by checking their skill requirements
     against the modifier's `filter_value`.
2. Build a skill-requirement lookup for charges and drones, same as currently
   done for modules.
3. Apply per-level vs role detection using the same `shipBonus*`/`eliteBonus*`
   naming convention.

**Affected ships (from SDE):**
- Drake: +10%/level kinetic missile damage
- Sacrilege: +5%/level HAM damage (all types)
- Gila: +500% role drone damageMultiplier, +10%/level missile kin/therm damage
- Rattlesnake: +50% role drone damageMultiplier, +25%/level cruise/torpedo damage
- Caracal, Cerberus, Raven, Tengu, Jackdaw, Osprey Navy, etc.

**Test:** Drake DPS should jump from ~172 to ~260+. Gila drone DPS from ~31 to ~180+.

**SDE gaps:** None — the modifiers are correctly stored in `sde_modifiers`, just
not processed.

---

### 3.3 — OwnerRequiredSkillModifier for damage mods (DDA, etc.)

**Gap:** Drone Damage Amplifier II uses `OwnerRequiredSkillModifier` with
`domain=charID` to boost drone `damageMultiplier` (attr 64) for items requiring
the Drones skill (3436). Its source attr is `droneDamageBonus` (1255 = 20.5%).
The engine's cross-module step only queries `domain=shipID`.

**Algorithm (plain language):**
DDA modifiers work like ship hull bonuses but the source is the damage mod, not
the ship. The mod's `droneDamageBonus` attribute is applied as `postPercent` to
the `damageMultiplier` attribute of all items (drones) owned by the character
that require the Drones skill. Multiple DDAs stack (stacking-penalized).

**Implementation approach:**
1. In the cross-module step, also query for `OwnerRequiredSkillModifier` with
   `domain=charID`.
2. For these modifiers, match against drones in the fit by checking their skill
   requirements (drones require the Drones skill).
3. Apply with stacking penalties (same as existing cross-module logic).

**Test:** Gila with 3x DDA II should show drone DPS increase of ~60% over
no-DDA baseline (before stacking penalty reduces 3rd DDA).

**SDE gaps:** None.

---

### 3.4 — OwnerRequiredSkillModifier for skill bonuses

**Gap:** Several combat skills use `OwnerRequiredSkillModifier` with `domain=charID`
to boost drone/charge attributes. The engine's `_apply_all_v_skill_bonuses()`
only queries `domain=shipID`.

**Key skills affected:**
- Drone Interfacing (3442): +10%/level drone damageMultiplier (OwnerRequiredSkillModifier,
  charID, attr 64, from attr 292=10.0, postPercent, skill 3436)
- Warhead Upgrades (20312): -5%/level missile explosion radius (OwnerRequiredSkillModifier,
  charID, attr 654, from attr 848=-5.0, postPercent, skill 3319)

**Algorithm (plain language):**
Same pattern as ship hull bonuses: the skill's bonus attribute is multiplied by
the skill level (always 5 for All-V assumption) and applied as postPercent to
matching items owned by the character. Drone Interfacing targets drones via the
Drones skill filter. Warhead Upgrades targets charges via the Missile Launcher
Operation skill filter.

**Implementation approach:**
1. In `_apply_all_v_skill_bonuses()`, also query for `OwnerRequiredSkillModifier`
   with `domain=charID`.
2. Build skill-requirement lookups for drones and charges.
3. Apply the bonus (base_value * 5) as postPercent to matching items.
4. Skills are never stacking-penalized.

**Test:** Drone DPS should increase ~50% from Drone Interfacing V. Missile
explosion radius should decrease 25% from Warhead Upgrades V (affects
application, not raw damage).

**SDE gaps:** None.

---

### 3.5 — Character-level missile damage multiplier (BCU mechanism)

**Gap:** BCU II applies its damage bonus via `ItemModifier` with `domain=charID`,
setting the character's `missileDamageMultiplier` (attr 212) to the BCU's
`missileDamageMultiplierBonus` (attr 213 = 1.1) using operator `preMul`.
The engine has no concept of character-level attributes.

**Algorithm (plain language):**
In EVE's dogma, the character entity has attributes that act as global multipliers.
`missileDamageMultiplier` (attr 212) defaults to 1.0 and is multiplied by each
BCU's bonus. With 3x BCU II: 1.0 * 1.1 * 1.1 * 1.1 = 1.331 (stacking-penalized).
The DPS formula for missiles is: `charge_damage * missileDamageMultiplier / cycle_time`.
The character's missileDamageMultiplier is a global scale on all missile charge damage.

**Implementation approach:**
1. Before the DPS calculation, collect all `ItemModifier` with `domain=charID`
   from fitted modules that target known character-level damage multiplier
   attributes (attr 212 = missileDamageMultiplier).
2. Accumulate these as multiplicative modifiers with stacking penalties.
3. In the DPS loop, multiply charge damage by the accumulated character-level
   multiplier when the weapon is a missile launcher.
4. Detect missile launchers by checking if they have the `missileEntityGroup`
   attribute or if they're in launcher-category groups.

**Known character-level damage attributes:**
- 212: missileDamageMultiplier (modified by BCU)

**Test:** Drake with 3x BCU II should have missile DPS multiplied by ~1.33x
(on top of ROF bonus).

**SDE gaps:** None — the modifier is correctly stored, just targeting domain=charID.

---

### 3.6 — Stacking penalty groups

**Gap:** All cross-module modifiers to the same attribute share one stacking pool.
Bastion ROF and damage mod ROF should not penalize each other.

**Algorithm (plain language):**
In Pyfa, each call to `multiply()` or `boost()` can specify a penalty group name.
Modifiers in different groups are sorted and penalized independently. When no
group is specified, all modifiers go to a default group. Common groups include:
- Default group for damage mods of the same type
- Separate groups for Bastion/Siege bonuses
- Separate groups for projected effects

Within each group, bonuses (multipliers > 1) and penalties (multipliers < 1) are
separated, sorted by magnitude, and the stacking penalty formula is applied per
position.

**Implementation approach:**
1. Add a `penalty_group` field to the modifier tracking in the cross-module step.
2. Use the source module's type_id as the penalty group key. This ensures copies
   of the same module stack with each other but different module types are
   independent.
3. Apply stacking penalties per group, then multiply all group results together.

**Test:** Fit with Bastion + 2x Heat Sinks: Bastion damage bonus should get
full effectiveness regardless of how many Heat Sinks are fitted.

**SDE gaps:** None.

---

### 3.7 — Capacitor discrete-event simulation

**Gap:** Current cap sim uses `peak_recharge >= drain` comparison with binary
search for stable percentage. This misses module interaction timing, stagger,
reload, and booster injection.

**Algorithm (plain language):**
The cap simulator uses a priority queue (min-heap) of module activation events.
Each event records when a module will next activate and how much cap it
drains/injects.

Setup: group identical modules to reduce event count. Optionally stagger their
first activation (turrets are NOT staggered, cap boosters are NOT staggered).

Main loop:
1. Pop the earliest event from the queue.
2. Regenerate cap from the last event time to now using EVE's cap regen formula:
   `cap = (1 + (sqrt(cap/maxCap) - 1) * exp((tLast - tNow) / tau))^2 * maxCap`
   where `tau = rechargeRate / 5.0`.
3. Subtract the module's cap drain (or add for cap boosters).
4. If cap goes negative, stop — unstable.
5. Schedule the module's next activation (current_time + cycle_time). If the
   module has a clip (limited ammo), add reload time when the clip is exhausted.
6. Track the lowest cap observed.

Stability detection: compute the LCM of all module cycle times. At each period
boundary, compare cap to the previous period. If cap >= previous, the fit is
stable (terminate early).

Cap booster logic: if injecting would exceed max cap, defer the injection until
cap drops enough. Prefer the injector that wastes the least.

**Implementation approach:**
1. New module: `app/fitting/cap_sim.py`
2. Build event list from fitted items that have `capacitorNeed` (attr 6) and
   `duration` (attr 73) or `speed` (attr 51).
3. Run the simulation up to 6 hours (21,600,000 ms).
4. Return: stable (bool), stable_percentage, lowest_cap, time_to_empty_if_unstable.
5. Keep the existing simple model as a fast-path estimate; use the full sim for
   the stats response.

**Test:** Drake fit should show accurate cap stability percentage. Rattlesnake
with active MWD + hardeners should show correct time-to-empty.

**SDE gaps:** None.

---

### 3.8 — Active tank (rep/s) display

**Gap:** No display of repair-per-second from armor/shield repairers.

**Algorithm (plain language):**
Sum the rep amount divided by cycle time for each active repair module. The rep
amount attribute depends on module type: `armorDamageAmount` (84) for armor
reppers, `shieldBonus` (68) for shield boosters. Cycle time is `duration` (73).

For effective rep rate, divide by the average resonance of the damage profile
to get effective HP repaired per second.

For sustainable rep rate (with cap sim), the cap simulator determines how long
each repper can run. Reps are ranked by efficiency (rep_amount / cap_cost). The
most efficient reps get priority for available cap headroom.

**Implementation approach:**
1. Add `armor_rep_rate`, `shield_rep_rate`, `hull_rep_rate` to the stats output.
2. For each active (online) repair module, compute: `rep_amount / (duration_ms / 1000)`.
3. For ancillary modules with paste loaded, multiply by the charge multiplier.
4. Sum per-layer.

**Test:** Sacrilege with Medium Armor Repairer II should show armor rep/s.

**SDE gaps:** None.

---

### 3.9 — EHP damage profiles

**Gap:** EHP always uses 25/25/25/25. Real damage patterns are rarely uniform.

**Algorithm (plain language):**
EHP per layer = raw_HP / weighted_average_resonance, where:
`weighted_resonance = sum(damage_fraction * resonance)` for each damage type.

Uniform (25/25/25/25) is a special case. Common profiles include:
- All EM: (1, 0, 0, 0)
- Guristas: (0, 0.39, 0.61, 0)
- Thermal hole: (0, 0, 0, 1)

**Implementation approach:**
1. Add a `damage_profile` parameter to the stats request (default: uniform).
2. Ship a set of built-in profiles (Guristas, Sansha, Serpentis, Angel, Blood,
   Sleeper, Trig, all-EM, all-therm, all-kin, all-expl).
3. Compute EHP per layer using the weighted resonance formula.

**Test:** Rattlesnake EHP against all-kinetic should be much higher than
against all-EM (shield kin resist is 74.6% vs EM 57.6%).

**SDE gaps:** Damage profiles are not in the SDE — they're hand-curated data.
Will ship as a constant dict in the engine.

---

### 3.10 — Lock time and minor stats

**Gap:** Lock time not calculated. A few other minor stats missing.

**Formulas:**
- Lock time: `40000 / scanResolution / asinh(targetSigRadius)^2`, capped at 30min
- Warp speed: `baseWarpSpeed * warpSpeedMultiplier * AU/s`

**Implementation approach:**
Add `lock_time` to stats output (requires a target sig radius parameter; default
to own sig radius for self-lock estimate).

**SDE gaps:** None.

---

## Test Strategy

Each port sub-phase adds pytest fixtures under `tests/fitting/`.

**Fixture format:** Each test fit is defined as a dict of `{ship_type_id, items}`.
Expected values are hand-computed from SDE attribute data and the correct modifier
formulas. Tests assert with tolerances:

| Stat | Tolerance |
|------|-----------|
| DPS (weapon, drone, total) | 0.5% |
| Volley | exact (integer) |
| EHP (per-layer, total) | 1% |
| Resists | 0.1% absolute |
| Cap stable (bool) | exact |
| Cap stable % | 2% absolute |
| CPU/PG used | exact to 0.1 |
| Speed, align, sig | 1% |

**Test fits:** Use the 6 fits from the gap report plus edge cases:
- Polarized module (force operator overrides resists to 0)
- T2 weapon with specialization skill bonus
- Spool-up weapon (verify fix from earlier)
- Multiple damage mod types (cross-type stacking)

Tests require database access (SDE data). Use a shared async fixture that opens
one `AsyncSessionLocal` for the test session. Mark tests with `@pytest.mark.asyncio`.

---

## SDE Data Gaps Flagged

| Gap | Description | Action |
|-----|-------------|--------|
| BCU damage attr | BCU II attr 213 (missileDamageMultiplierBonus) is not a standard damage modifier in modifierInfo — it targets character-level attr 212 via ItemModifier/charID | Handled by port item 3.5 (character-level multipliers) |
| Damage profiles | Not in SDE; NPC damage patterns are community-maintained | Ship as constants |
| Effect category 1 (active) modules | Some shield hardeners may have resist effects in category 1 rather than 0/4 | Audit needed during 3.2 — may need to expand PASSIVE_EFFECT_CATS |

---

## Priority / Dependency Map

```
3.1 (hull resists) ──────────────────────────────── standalone, trivial
3.2 (ship hull OwnerRequired) ──┐
3.3 (DDA OwnerRequired)     ────┤── share infra for OwnerRequiredSkillModifier
3.4 (skill OwnerRequired)   ────┘
3.5 (char-level BCU)         ──────────────────────── independent, moderate
3.6 (stacking groups)        ──────────────────────── independent, small
3.7 (cap sim)                ──────────────────────── independent, large
3.8 (active tank)            ──── depends on 3.7 ──── for sustainable tank
3.9 (damage profiles)        ──────────────────────── independent, small
3.10 (lock time etc.)        ──────────────────────── independent, trivial
```

Recommended execution order: 3.1 → 3.2+3.3+3.4 (together) → 3.5 → 3.6 → 3.7 → 3.8 → 3.9 → 3.10
