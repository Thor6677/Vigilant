# Pyfa Audit Round 2 — Reference Sources

## Pyfa

- **Version:** v2.66.2
- **Release tag:** `v2.66.2`
- **GitHub commit:** `2651316` (extracted directory: `pyfa-org-Pyfa-2651316`)
- **Local path:** `/tmp/references/pyfa/pyfa-org-Pyfa-2651316/`
- **Relevant subtree:** `eos/` (132 Python files)

### Key eos files

| File | Covers |
|------|--------|
| `eos/modifiedAttributeDict.py` | Modifier accumulation, operator ordering, stacking penalties |
| `eos/calc.py` | Calculation dispatch, fit recalculation |
| `eos/capSim.py` | Capacitor simulation (discrete-event) |
| `eos/const.py` | Operator constants, effect category IDs |
| `eos/saveddata/fit.py` | Fit-level stats: DPS, EHP, tank, cap, navigation |
| `eos/saveddata/module.py` | Module state, volley/DPS, cycle time, slot handling |
| `eos/saveddata/drone.py` | Drone DPS, bandwidth, tracking |
| `eos/saveddata/ship.py` | Ship attribute access, slot counts |
| `eos/saveddata/character.py` | Skill application, All V logic |
| `eos/effectHandlerHelpers.py` | Effect handler registration, gang/system effects |
| `eos/effects.py` | Effect implementations |

## Theorycrafter

- **Version:** v1.9.6-ag7
- **License:** View-only (see `/tmp/references/theorycrafter/index.md` for terms)
- **Local path:** `/tmp/references/theorycrafter/source/`
- **Relevant subtree:** `FittingEngine/src/commonMain/kotlin/theorycrafter/fitting/`

### Key fitting engine files

| File | Covers |
|------|--------|
| `FittingEngine.kt` | Modifier application order, property recomputation, skill bonuses, T3C subsystem handling |
| `StackingPenalty.kt` | Stacking penalty formula, sign-based group splitting |
| `Mechanics.kt` | DPS, turret/missile damage application, cap sim, recharge, EHP, align, lock time |
| `ItemDefense.kt` | Shield/Armor/Structure layers, resonances, EHP, repair rates |
| `Module.kt` | Module states, volley/DPS, slot properties |
| `ModuleOrDrone.kt` | Base class: damage, tracking, repair |
| `Subsystem.kt` | T3C subsystem bonuses per faction and slot kind |
| `Fit.kt` | Fit-level aggregation |
| `SpoolupCycles.kt` | Triglavian spool-up mechanics |
