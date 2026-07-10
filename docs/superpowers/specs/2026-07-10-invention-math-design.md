# Invention Expected-Cost in Build Finder — Design Spec

**Date:** 2026-07-10 · **Ticket:** T-041 item 3 · **Status:** approved

Build Finder ranks manufacturing margin only; T2 items are priced off their
raw BPC material list, ignoring what it costs to *get* the BPC (datacores,
decryptors, failed attempts). Add expected invention cost so T2 rankings are
honest.

## Decisions (made with user)

1. **Skills come from a character selector** (fitting-tool pattern) resolving
   each blueprint's actual required skill levels from the character's ESI
   skills; missing skills count as level 0 and flag the row. Fallback when no
   character is selected: two manual selects applied uniformly — encryption
   level and science level (0–5).
2. **Decryptor picker**: chip row, "No decryptor" default + the 8 decryptors;
   the whole ranking recomputes under the selection (no per-row auto-pick).

## Architecture

### 1. SDE extension — same `blueprints.jsonl`, new parse pass (no new download)

Three new tables (auto-create; reimport via the documented SDE Reload
procedure):

- `sde_blueprint_invention(blueprint_type_id PK, product_blueprint_type_id,
  probability, base_runs, time)` — from `activities.invention.products[0]`
  (`typeID`, `probability`, `quantity` = base invented runs).
- `sde_blueprint_invention_materials(blueprint_type_id, material_type_id,
  quantity)` — datacores.
- `sde_blueprint_invention_skills(blueprint_type_id, skill_type_id)` — the
  encryption + science skills (from `activities.invention.skills`).

Note `blueprint_type_id` here is the **T1 blueprint** being invented FROM;
`product_blueprint_type_id` is the **T2 blueprint**. Chain to the sellable
product via existing `SDEBlueprintInfo` (T2 blueprint → product_type_id).

Decryptors: group_id 1304 items; modifiers live in the already-imported
`sde_type_dogma_attributes` (probability multiplier / ME / TE / run
modifiers — **verify exact attribute IDs against the SDE during
implementation**; expected ≈ 1112–1114, 1124).

### 2. Math — `app/industry/invention.py` (new, pure)

```
P            = base_prob × (1 + E/40 + (S1+S2)/30) × decryptor_prob_mult
attempt_cost = Σ(datacore_qty × price) + decryptor_price   (consumed per attempt)
invented_runs= base_runs + decryptor_run_mod
invented_me  = 2 + decryptor_me_mod          (T2 BPC base ME2/TE4)
overhead/unit= attempt_cost / P / (invented_runs × per_run_output_qty)
```

- Skill levels clamp 0–5; P clamps to (0, 1].
- Any unpriced datacore/decryptor → overhead is None → row excluded-and-
  counted (site-wide unpriced convention).
- Unit-tested against hand-computed numbers including a published reference
  case (e.g. Damage Control II, all-IV, no decryptor).

### 3. Build Finder integration

- Inventability detection: product → T2 blueprint (SDEBlueprintInfo) → row in
  sde_blueprint_invention pointing at it. Non-inventable items unchanged.
- For inventable rows: T2 build cost computed at `invented_me` (overrides the
  page ME assumption for those rows only), then + overhead/unit. Row
  breakdown shows build cost and invention overhead separately.
- Skill resolution: selected character's trained levels per blueprint's
  required skills (bulk skill fetch, one ESI call — reuse the fitting tool's
  skill-fetch helper); manual mode maps encryption-select + science-select
  uniformly.

### 4. Route/UI — `/industry/build-finder`

- Character dropdown (reuse `/tools/fitting/characters`-style source, skills
  scope required) + the two fallback selects + decryptor chip row.
- Rows flagged when skills resolved to 0 (character missing a science).
- Footnote: probability formula, ME2/TE4 base, copy-cost exclusion (T1 BPC
  copy cost deliberately out of scope v1 — negligible for most items, noted).

## Error handling

- SDE tables empty (pre-reimport): invention silently disabled, footnote says
  "invention data not yet imported" — page never breaks.
- Character without skills scope: fall back to manual selects with a notice.

## Testing

- Parser: fixture blueprints.jsonl entries (incl. an item with no invention
  activity) → expected rows.
- Math: hand-computed cases; decryptor modifier application; clamps;
  unpriced propagation.
- Integration: fixture SDE rows → ranking includes overhead; non-inventable
  row unchanged; skill-flagging.

## Execution notes

Opus: invention math module + probability/decryptor correctness + reference
verification. Sonnet: SDE models/parse pass, route/UI wiring, character
dropdown. Deploy: rebuild → deploy → delete `sde_meta` last_updated row →
restart (triggers reimport; ~minutes, runs in startup background per existing
loader). Verify new tables populated before enabling ranking.
