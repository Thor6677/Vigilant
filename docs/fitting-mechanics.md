# EVE Online Fitting Mechanics Brief

Reference document for the Vigilant fitting engine. Every formula below is verified against Pyfa source code and EVE University wiki.

---

## 1. Modifier Application Pipeline

EVE's Dogma engine applies modifiers in a strict order. Pyfa's implementation (from `ModifiedAttributeDict.__calculateValue()`):

```
1. FORCE      — if any modifier forces a value, return it (subject to cap)
2. base       — intermediary > preAssign > original > default > 0
3. += all PREINCREASE values (flat adds before multiplication)
4. *= all non-penalized MULTIPLY factors (single product)
5. *= stacking-penalized MULTIPLY factors (per group, see below)
6. += all POSTINCREASE values (flat adds after multiplication)
7. apply attribute cap (if maxAttributeID defined)
8. round CPU/PG to 2 decimals
```

**Operator IDs** (from Pyfa `eos/const.py`):

| ID | Name | Description |
|----|------|-------------|
| 0 | PREASSIGN | Overwrite base value |
| 1 | PREINCREASE | Add before multiply |
| 2 | MULTIPLY | Multiplicative (stacking-penalized if applicable) |
| 3 | POSTINCREASE | Add after multiply |
| 4 | FORCE | Lock value, no further modification |

`boost(attr, X)` is sugar for `multiply(attr, 1 + X/100)`.

---

## 2. Stacking Penalties

### Formula

The Nth stacking-penalized modifier (0-indexed) is scaled by:

```
S(n) = e^(-(n / 2.67)^2)  =  e^(-n^2 / 7.1289)
```

| Position | Effectiveness |
|----------|--------------|
| 1st (n=0) | 100.0% |
| 2nd (n=1) | 86.9% |
| 3rd (n=2) | 57.1% |
| 4th (n=3) | 28.3% |
| 5th (n=4) | 10.6% |
| 6th (n=5) | 3.0% |

### Ordering

Modifiers are sorted by **absolute strength descending** (strongest first). Bonuses (>1.0) and penalties (<1.0) are penalized **separately** within the same stacking group.

From Pyfa `eos/calc.py`:
```python
val *= 1 + (bonus - 1) * math.exp(-i**2 / 7.1289)
```

### What IS Penalized

Percentage modifiers from **modules, rigs, command bursts, environmental effects** on:
- Shield/armor/hull resistances
- Velocity, inertia, signature radius
- Tracking speed, optimal range, falloff
- Damage multiplier, rate of fire
- Scan resolution, targeting range, sensor strength
- Missile explosion velocity/radius
- Drone damage

### What is EXEMPT

- **Skills** — never penalized
- **Ship hull bonuses** (per-level and role) — never penalized
- **Implants and hardwirings** — never penalized
- **Boosters** — never penalized
- **Flat/absolute bonuses** (+1000 HP, +15 GJ cap) — never penalized
- CPU/PG output and usage — not penalized
- HP values (both flat and percentage) — not penalized
- Capacitor capacity and recharge — not penalized
- Cargo capacity — not penalized

### Special Cases

- **Damage Control / ADC**: Gets its own stacking group (position 0), does not count against other resistance modules.
- **Velocity**: Two separate stacking groups — propulsion modules (AB/MWD) and propulsion upgrades (Nanofiber/Overdrive) stack independently.
- **Overheat**: Turret/missile damage and ROF overheat bonuses are NOT penalized. Shield/armor rep overheat IS penalized.

*Sources: Pyfa `eos/calc.py`, EVE University Stacking Penalties wiki*

---

## 3. Capacitor

### Recharge Formula

```
dC/dt = (10 * Cmax / tau) * (sqrt(C/Cmax) - C/Cmax)
```

Where `Cmax` = max capacitor (GJ), `tau` = recharge time (seconds, attr 55 is in ms).

**Peak recharge** at exactly **25% capacitor**:

```
peak_recharge = 2.5 * Cmax / tau
```

### Closed-form Cap Over Time

```
C(t1) = Cmax * (1 + (sqrt(C0/Cmax) - 1) * e^(5*(t0-t1)/tau))^2
```

### Cap Stability Simulation (Pyfa approach)

Pyfa uses a **discrete event simulation** (`eos/capSim.py`):

1. Each active module is an event: `(duration_ms, capNeed_GJ, clipSize, reloadTime)`
2. Priority queue sorted by next activation time
3. Between activations, cap regenerates via closed-form formula
4. At each activation, subtract `capNeed` from current cap
5. If cap goes negative → **unstable**, report time until depletion
6. If cap after a full cycle >= cap at start of cycle → **stable**, report stable percentage
7. Sim runs up to 6 hours max

### Relevant Attributes

| Attr ID | Name | Unit |
|---------|------|------|
| 482 | capacitorCapacity | GJ |
| 55 | rechargeRate | milliseconds |
| 6 | capacitorNeed | GJ per activation |
| 73 | duration | ms (cycle time) |

*Sources: Pyfa `eos/saveddata/fit.py`, `eos/capSim.py`, EVE Uni Capacitor wiki*

---

## 4. Effective HP (EHP)

### Per-layer EHP

For damage profile `(d_em, d_th, d_kin, d_exp)` where `D = sum`:

```
EHP_layer = HP / sum( (d_type / D) * resonance_type )
```

Where `resonance = 1 - resistance_percentage/100`. A resonance of 0.3 = 70% resist.

### Single Damage Type

```
EHP_em = HP / resonance_em
```

### Uniform EHP (25/25/25/25)

```
EHP_uniform = HP / avg(res_em, res_th, res_kin, res_exp)
```

### Total EHP

```
Total_EHP = EHP_shield + EHP_armor + EHP_hull
```

### Resistance Stacking

Resistances are stored as **resonances** (multiplicative). Multiple resistance modules multiply resonances:

```
final_resonance = base_resonance * mod1_resonance * mod2_resonance * ...
```

With stacking penalties applied to module contributions (sorted by strength, penalized per group).

### Relevant Attributes

| Layer | HP Attr | EM Res | Therm Res | Kin Res | Expl Res |
|-------|---------|--------|-----------|---------|----------|
| Shield | 263 | 271 | 274 | 273 | 272 |
| Armor | 265 | 267 | 270 | 269 | 268 |
| Hull | 9 | 974 | 977 | 976 | 975 |

*Source: Pyfa `eos/saveddata/damagePattern.py`*

---

## 5. DPS Calculation

### Turret DPS

```
volley = (emDmg + thermDmg + kinDmg + explDmg) * damageMultiplier
DPS = volley / (cycleTime_ms / 1000)
```

Damage values come from the **charge** (ammo). `damageMultiplier` comes from the **turret** (modified by skills, ship bonuses, damage mods). `cycleTime` is the module's `duration` attribute.

### Rate of Fire

ROF bonuses **reduce** cycle time. A -10% ROF bonus multiplies cycle time by 0.9 (faster). Lower cycle time = higher DPS.

### Missile DPS

```
volley = baseDamage  (from missile type)
DPS = volley / (launcher_cycleTime_ms / 1000)
```

Application is separate — see Tracking section below.

### Drone DPS

```
DPS_per_drone = (emDmg + thermDmg + kinDmg + explDmg) * damageMultiplier / (cycleTime_ms / 1000)
total_drone_DPS = sum(DPS_per_drone * active_count)
```

### Relevant Attributes

| Attr ID | Name | On |
|---------|------|----|
| 114 | emDamage | charge/missile |
| 118 | thermalDamage | charge/missile |
| 117 | kineticDamage | charge/missile |
| 116 | explosiveDamage | charge/missile |
| 64 | damageMultiplier | turret/drone |
| 51 | speed (ROF) | turret/launcher |
| 73 | duration | module cycle time |

*Source: Pyfa `eos/saveddata/module.py`*

---

## 6. Tracking and Application

### Turret Hit Chance

```
ChanceToHit = 0.5 ^ (trackingTerm^2 + rangeTerm^2)
```

Where:
```
trackingTerm = angularVelocity * 40000 / (trackingSpeed * targetSigRadius)
rangeTerm = max(0, distance - optimalRange) / falloffRange
```

The `40000` is the legacy turret signature resolution constant.

### Turret Damage Distribution

- Hit if random `r < ChanceToHit`
- On hit: damage = baseDamage * (r + 0.49), range 0.49x to 1.49x
- Wrecking hit: if `r < 0.01`, damage = baseDamage * 3.0

### Missile Application

```
applied_damage = D * min(1, S/E, (S/E * Ve/Vt)^drf)
```

Where:
- `S` = target signature radius
- `E` = missile explosion radius (attr 654)
- `Ve` = missile explosion velocity (attr 653)
- `Vt` = target absolute velocity (NOT angular)
- `drf` = damage reduction factor (attr 1353, varies by missile type: 0.56-1.0)

When target is stationary, full damage always applies.

### Drone Tracking

Drones use the **turret formula** — they have their own tracking speed, optimal, and falloff.

### Relevant Attributes

| Attr ID | Name |
|---------|------|
| 160 | trackingSpeed |
| 54 | maxRange (optimal) |
| 158 | falloff |
| 654 | aoeCloudSize (explosion radius) |
| 653 | aoeVelocity (explosion velocity) |
| 1353 | aoeDamageReductionFactor |
| 552 | signatureRadius (target) |

*Sources: EVE Uni Turret Mechanics wiki, Pyfa `eos/calc.py`*

---

## 7. Align Time

```
alignTime = -ln(0.25) * inertia * mass / 1,000,000
          = ln(4) * inertia * mass / 1,000,000
          ≈ 1.38629 * inertia * mass / 1,000,000
```

Result in seconds. Server-tick align time is `ceil(alignTime)`.

Attributes: `agility` (70), `mass` (4).

*Source: Pyfa `eos/saveddata/fit.py`*

---

## 8. Shield Passive Recharge

Same curve as capacitor:

```
dS/dt = (10 * Smax / tau_shield) * (sqrt(S/Smax) - S/Smax)
peak_shield_recharge = 2.5 * shieldCapacity / shieldRechargeTime
```

Peak at **25% shield**. Armor and hull do NOT passively recharge.

Attributes: `shieldCapacity` (263), `shieldRechargeRate` (479, in ms).

---

## 9. Lock Time

```
lockTime = min(40000 / scanResolution / asinh(targetSigRadius)^2, 1800)
```

Capped at 30 minutes. Attributes: `scanResolution` (564), target `signatureRadius` (552).

*Source: Pyfa `eos/calc.py`*

---

## 10. Warp Speed

```
warpSpeed = baseWarpSpeed * warpSpeedMultiplier
```

Most ships: `baseWarpSpeed` (1281) = 1.0, so effective speed = `warpSpeedMultiplier` (600).

Max warp distance (single cap charge):
```
maxWarpDist = capacitorCapacity / (mass * warpCapacitorNeed)
```

Attribute: `warpCapacitorNeed` (153).

---

## 11. Ship Bonuses

### Per-level Skill Bonuses

Ship bonus effects read a bonus attribute from the ship and multiply by character skill level:

```python
# Example: +5% kinetic missile damage per Caldari Battlecruiser level
bonus = ship.getAttr('shipBonusCBC') * character.skillLevel('Caldari Battlecruiser')
# Applied as: boost('kineticDamage', bonus)  →  multiply(1 + bonus/100)
```

Naming convention in SDE:
- `shipBonus{Race}{Class}` — base hull per-level bonus
- `eliteBonus{Class}{N}` — T2/faction per-level bonus
- `roleBonus{Class}` — flat role bonus (no skill multiplier)

### Key Fitting Skills

| Skill | Per Level | Effect |
|-------|-----------|--------|
| CPU Management | +5% CPU output | `boostItemAttr('cpuOutput', 5 * level)` |
| Power Grid Management | +5% PG output | `boostItemAttr('powerOutput', 5 * level)` |
| Electronics Upgrades | -5% CPU for eligible mods | `filteredItemBoost(requiresSkill, 'cpu', -5 * level)` |
| Weapon Upgrades | -5% CPU for weapons | `filteredItemBoost(requiresSkill('Gunnery'), 'cpu', -5 * level)` |
| Advanced Weapon Upgrades | -2% PG for weapons | `filteredItemBoost(..., 'power', -2 * level)` |

Ship bonuses are **not** stacking penalized. They apply before module stacking penalty calculation.

---

## 12. Module States

| State | Value | Effects Applied |
|-------|-------|-----------------|
| OFFLINE | -1 | None |
| ONLINE | 0 | Passive (cat 0) + Online (cat 4) |
| ACTIVE | 1 | Above + Active (cat 1) |
| OVERHEATED | 2 | Above + Overload (cat 5) |

Effect `type` property gates which state is required: `'passive'`, `'active'`, `'overheat'`.

`runTime` controls phase ordering: `'early'` (Bastion/Siege/ADC), `'normal'` (most), `'late'` (active reps).

---

## 13. SDE Tables Required

### Currently Imported

| Table | SDE Source | Purpose |
|-------|-----------|---------|
| `sde_types` | types.jsonl | Item definitions (typeID, name, group, market group) |
| `sde_groups` | groups.jsonl | Group classification (groupID, categoryID) |
| `sde_market_groups` | marketGroups.jsonl | Market hierarchy for browsing |
| `sde_type_dogma_attrs` | typeDogma.jsonl (dogmaAttributes array) | Per-type attribute values |
| `sde_dogma_attributes` | dogmaAttributes.jsonl | Attribute definitions (name, stackable, highIsGood) |
| `sde_module_slots` | typeDogma.jsonl (dogmaEffects array) | Pre-computed slot type from effects |

### Missing — Needed for Full Simulation

| Table | SDE Source | Purpose |
|-------|-----------|---------|
| **dgmEffects** | dogmaEffects.jsonl | Effect definitions (category, modifierInfo YAML, is_offensive, discharge/duration attrs) |
| **dgmTypeEffects** | typeDogma.jsonl (dogmaEffects array) | Which effects each type has + isDefault flag |

The `modifierInfo` YAML on each effect defines what attribute is modified, by what source attribute, using what operator, with what domain/filter. This replaces the legacy `dgmExpressions` table.

### ESI Endpoints

| Endpoint | Scope | Purpose |
|----------|-------|---------|
| `GET /characters/{id}/skills/` | `esi-skills.read_skills.v1` | Character skill levels |
| `GET /characters/{id}/implants/` | `esi-clones.read_implants.v1` | Active implant type IDs |
| `GET /characters/{id}/fittings/` | `esi-fittings.read_fittings.v1` | In-game saved fittings |

---

## 14. Missing Attribute IDs (Not in constants.py)

### Weapon/Damage
- 51: speed (rate of fire, ms)
- 54: maxRange (optimal range)
- 64: damageMultiplier
- 114/116/117/118: emDamage/explosiveDamage/kineticDamage/thermalDamage
- 158: falloff
- 160: trackingSpeed
- 653: aoeVelocity (explosion velocity)
- 654: aoeCloudSize (explosion radius)
- 1353: aoeDamageReductionFactor

### Module Activation
- 6: capacitorNeed (cap per activation)
- 73: duration (cycle time, ms)

### Repair/Boost
- 68: shieldBonus (per cycle)
- 84: armorDamageAmount (armor repaired per cycle)

### Navigation
- 20: speedFactor (velocity bonus %)
- 600: warpSpeedMultiplier
- 479: shieldRechargeRate (ms)

### Ship Layout
- 352: maxActiveDrones

### Charge Constraints
- 128: chargeSize
- 604-610: chargeGroup1-5

---

## 15. Implementation Strategy: Pyfa's Approach

Pyfa does **not** evaluate CCP's expression trees. Instead, each of the ~6,000 SDE effects has a hand-coded Python handler class (`Effect{ID}`) in a single 43,000-line file. Each handler explicitly calls modifier methods on the fit.

For Vigilant, the practical approach is:

1. **Phase A (current)**: Base stats + resource usage only. No modifiers. *(This is where we are now.)*
2. **Phase B**: Import `modifierInfo` from effects. Parse the YAML to build a modifier registry: "effect X on module type Y modifies attribute Z with operator W using source attribute V". Apply modifiers in pipeline order without hand-coding each effect.
3. **Phase C**: Add stacking penalties using `stackable` flag from attribute definitions + stacking group from modifier metadata.
4. **Phase D**: Add skill bonuses by querying character skills from ESI and applying skill-level-scaled modifiers.
5. **Phase E**: Cap stability simulation, DPS calculation, EHP display.

The `modifierInfo` YAML approach (Phase B) is significantly less work than Pyfa's hand-coded effects and covers ~90% of modules correctly. Edge cases (ADC, Reactive Armor Hardener, Bastion, spool-up weapons) can be special-cased later.

---

## 16. Edge Cases

- **Assault Damage Control**: Uses FORCE operator when active — bypasses stacking. `runTime='early'`.
- **Reactive Armor Hardener**: Shifts resist profile toward incoming damage over ~50 cycles to find equilibrium.
- **Bastion/Siege/Triage**: `runTime='early'`, modifies many attributes, forces immobility.
- **Ancillary modules**: Separate cap/paste fuel modes with different rep amounts.
- **Spool-up weapons** (Triglavian): `damageMultiplierBonusPerCycle` ramps damage over time.
- **Mutated (Abyssal) modules**: Override base attributes with rolled values.
- **T3C subsystems**: Mode items with passive effects that swap on mode change.
- **Cap boosters**: Flagged as `isInjector` in cap sim — fire on-demand, not staggered.

---

*Document generated from: Pyfa source code (github.com/pyfa-org/Pyfa), EVE University wiki (wiki.eveuniversity.org), CCP SDE documentation.*
