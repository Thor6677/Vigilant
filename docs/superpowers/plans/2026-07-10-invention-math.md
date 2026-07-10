# Invention Expected-Cost Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Execution model:** Task 2 → **Opus** (probability/overhead math, correctness-critical). Tasks 1, 3, 4 → **Sonnet** (SDE parse, route/UI). **Fable reviews each task's diff + test output before the next task starts.** Task 5 is a coordinator runbook.

**Goal:** Build Finder folds expected invention cost (datacores, decryptors, failed attempts) into T2 rankings, with skills from a selected character or manual level selects.

**Architecture:** New parse pass over the already-downloaded `blueprints.jsonl` fills three invention tables; a pure math module computes P and per-unit overhead; the Build Finder route resolves skills (character ESI skills or manual selects) and a decryptor choice, then adds overhead to inventable rows. Spec: `docs/superpowers/specs/2026-07-10-invention-math-design.md`.

**Tech Stack:** FastAPI + SQLAlchemy async (SQLite), Jinja2/htmx. SDE via `app/sde/loader.py` (`_iter_jsonl` over `blueprints.jsonl`). Tests: pytest.

**Key formula (single source of truth in `app/industry/invention.py`):**
```
P             = base_prob × (1 + E/40 + (S1+S2)/30) × decryptor_prob_mult   (clamped to (0, 1])
attempt_cost  = Σ(datacore_qty × price) + decryptor_price
invented_runs = base_runs + decryptor_run_mod
invented_me   = 2 + decryptor_me_mod
overhead/unit = attempt_cost / P / (invented_runs × per_run_output_qty)
```

---

### Task 1: SDE invention tables + loader parse pass (Sonnet)

**Goal:** Invention info/materials/skills importable from `blueprints.jsonl`.

**Files:**
- Modify: `app/db/sde_models.py` (three models, after `SDEBlueprintInfo`)
- Modify: `app/sde/loader.py` (new parse block after the blueprint-info block ending ~line 449)
- Test: `tests/test_sde_invention_parse.py` (new)

**Acceptance Criteria:**
- [ ] Models: `SDEBlueprintInvention(blueprint_type_id Integer PK, product_blueprint_type_id Integer index, probability Float, base_runs Integer, time Integer nullable)`; `SDEBlueprintInventionMaterial(id Integer PK autoincrement, blueprint_type_id Integer index, material_type_id Integer, quantity Integer)`; `SDEBlueprintInventionSkill(id Integer PK autoincrement, blueprint_type_id Integer index, skill_type_id Integer)`.
- [ ] Loader block mirrors the existing blueprint blocks: `DELETE FROM` each table, iterate `_iter_jsonl(zf, "blueprints.jsonl")`, take `item.get("activities", {}).get("invention")`, first product's `typeID/probability/quantity` (quantity = base invented runs, default 1; skip items with no products), materials list, `skills` list (`[{"typeID":..., "level":...}]` — store typeID only). Batched `_bulk_insert` at 500, count logged.
- [ ] Parse logic extracted as a pure function so it's testable without a zipfile: `_parse_invention_item(item) -> tuple[dict | None, list[dict], list[dict]]` used by the loader loop.
- [ ] Tests: fixture item with invention (2 datacores, 2 skills + encryption, probability 0.34, product quantity 10) → expected three row sets; item without invention → `(None, [], [])`; item with invention but no products → `(None, [], [])`.

**Verify:** `.venv/bin/python -m pytest tests/test_sde_invention_parse.py -q` → pass; `python3 -c "import ast; ast.parse(open('app/sde/loader.py').read())"`.

**Steps:**

- [ ] **Step 1: Failing tests**

```python
from app.sde.loader import _parse_invention_item


FIXTURE = {
    "_key": "687",  # Rifter Blueprint
    "activities": {"invention": {
        "materials": [{"typeID": 20410, "quantity": 8},
                      {"typeID": 20424, "quantity": 8}],
        "products": [{"typeID": 11373, "probability": 0.3, "quantity": 1}],
        "skills": [{"typeID": 3402, "level": 1}, {"typeID": 11433, "level": 1},
                   {"typeID": 21791, "level": 1}],
        "time": 63900,
    }},
}


def test_parse_invention_item_full():
    info, mats, skills = _parse_invention_item(FIXTURE)
    assert info == {"blueprint_type_id": 687, "product_blueprint_type_id": 11373,
                    "probability": 0.3, "base_runs": 1, "time": 63900}
    assert {m["material_type_id"] for m in mats} == {20410, 20424}
    assert all(m["blueprint_type_id"] == 687 for m in mats)
    assert {s["skill_type_id"] for s in skills} == {3402, 11433, 21791}


def test_parse_invention_item_absent():
    assert _parse_invention_item({"_key": "1", "activities": {}}) == (None, [], [])


def test_parse_invention_item_no_products():
    item = {"_key": "1", "activities": {"invention": {"materials": [], "skills": []}}}
    assert _parse_invention_item(item) == (None, [], [])
```

- [ ] **Step 2: Run → ImportError.**
- [ ] **Step 3: Implement** models + `_parse_invention_item` + loader block:

```python
def _parse_invention_item(item: dict):
    """(info_row | None, material_rows, skill_rows) for one blueprints.jsonl
    entry's invention activity. CCP field names: typeID / quantity /
    probability (see the SDE JSONL gotchas memory — no 'operator' style
    surprises here, but keys are camelCase)."""
    inv = item.get("activities", {}).get("invention")
    if not inv:
        return None, [], []
    products = inv.get("products") or []
    if not products:
        return None, [], []
    try:
        bp_id = int(item["_key"])
        p0 = products[0]
        info = {
            "blueprint_type_id": bp_id,
            "product_blueprint_type_id": int(p0["typeID"]),
            "probability": float(p0.get("probability") or 0.0),
            "base_runs": int(p0.get("quantity") or 1),
            "time": inv.get("time"),
        }
    except (KeyError, ValueError, TypeError):
        return None, [], []
    mats = [{"blueprint_type_id": bp_id,
             "material_type_id": int(m["typeID"]),
             "quantity": int(m["quantity"])}
            for m in inv.get("materials", [])
            if m.get("typeID") and m.get("quantity")]
    skills = [{"blueprint_type_id": bp_id, "skill_type_id": int(s["typeID"])}
              for s in inv.get("skills", []) if s.get("typeID")]
    return info, mats, skills
```

Loader block (insert after the blueprint-info block, same batching style; remember the import of the three new models at the top of loader.py).

- [ ] **Step 4: Verify** tests + full suite.
- [ ] **Step 5: Commit**

```bash
git add app/db/sde_models.py app/sde/loader.py tests/test_sde_invention_parse.py
git commit -m "feat(sde): import invention activities (info/datacores/skills)"
```

---

### Task 2: Pure invention math — `app/industry/invention.py` (Opus)

**Goal:** Probability + expected per-unit overhead, decryptor modifiers applied, hand-verified against a published reference case.

**Files:**
- Create: `app/industry/invention.py`
- Test: `tests/test_invention_math.py` (new)

**Acceptance Criteria:**
- [ ] `invention_probability(base_prob, encryption_lvl, sci1_lvl, sci2_lvl, decryptor_prob_mult=1.0) -> float` — formula above, levels clamped 0–5, result clamped to (0.0, 1.0]; returns 0.0 only when base_prob ≤ 0 (caller treats as un-inventable).
- [ ] `invention_overhead_per_unit(attempt_cost, probability, invented_runs, per_run_output_qty) -> float | None` — None when probability ≤ 0 or runs/output < 1.
- [ ] `attempt_cost(datacores, price_map, decryptor_price=0.0) -> float | None` — None if any datacore unpriced (site-wide unpriced convention). `datacores: [{"material_type_id", "quantity"}]`.
- [ ] `DECRYPTORS: dict[str, Decryptor]` constant keyed by slug ("attainment", "augmentation", …) where `Decryptor` is a frozen dataclass with `type_id: int`, `name: str`, `prob_mult: float`, `me_mod: int`, `te_mod: int`, `run_mod: int` (attribute access — matches the `invented_bpc` tests) — **populate values from the SDE dogma attributes during implementation** (read `sde_type_dogma_attributes` on prod read-only or from the published attribute table; expected attribute IDs ≈ 1112 probabilityMultiplier / 1113 inventionMEModifier / 1114 inventionTEModifier / 1124 inventionMaxRunModifier — verify and record the verified IDs in a comment). Include the "no decryptor" sentinel `None` handling in call sites, not the table.
- [ ] `invented_bpc(base_runs, decryptor) -> (runs, me)` — `(base_runs + run_mod, 2 + me_mod)`, floors at (1, 0).
- [ ] Reference test: Damage Control II invention from Damage Control I BPC — base_prob 0.34 (module class), skills all IV, no decryptor: `P = 0.34 × (1 + 4/40 + 8/30) = 0.34 × 1.3667 = 0.4647` (±0.0001). Overhead with fixture datacore prices asserts the full chain.

**Verify:** `.venv/bin/python -m pytest tests/test_invention_math.py -q` → pass.

**Steps:**

- [ ] **Step 1: Failing tests**

```python
import pytest

from app.industry.invention import (
    attempt_cost, invented_bpc, invention_overhead_per_unit,
    invention_probability,
)


def test_probability_reference_dcii_all_iv_no_decryptor():
    p = invention_probability(0.34, 4, 4, 4)
    assert p == pytest.approx(0.34 * (1 + 4/40 + (4+4)/30), abs=1e-4)  # 0.46466...


def test_probability_clamps():
    assert invention_probability(0.34, 9, 9, 9) <= 1.0          # level clamp → 5s
    assert invention_probability(0.34, 5, 5, 5) == pytest.approx(
        0.34 * (1 + 5/40 + 10/30))
    assert invention_probability(0.0, 4, 4, 4) == 0.0
    assert invention_probability(2.0, 5, 5, 5) == 1.0            # P clamp


def test_attempt_cost_unpriced_datacore_is_none():
    dcs = [{"material_type_id": 20410, "quantity": 8},
           {"material_type_id": 20424, "quantity": 8}]
    assert attempt_cost(dcs, {20410: 100.0}) is None
    assert attempt_cost(dcs, {20410: 100.0, 20424: 50.0},
                        decryptor_price=1000.0) == pytest.approx(
        8*100 + 8*50 + 1000)


def test_invented_bpc_mods_and_floors():
    class D:  # minimal decryptor stub
        run_mod, me_mod = +2, -1
    assert invented_bpc(1, None) == (1, 2)
    assert invented_bpc(1, D) == (3, 1)
    class Harsh:
        run_mod, me_mod = -9, -9
    assert invented_bpc(1, Harsh) == (1, 0)   # floors


def test_overhead_full_chain():
    # attempt 1300 ISK, P=0.4647, 1 run/success, 1 unit/run
    o = invention_overhead_per_unit(1300.0, 0.46466, 1, 1)
    assert o == pytest.approx(1300.0 / 0.46466)
    assert invention_overhead_per_unit(1300.0, 0.0, 1, 1) is None
    assert invention_overhead_per_unit(1300.0, 0.5, 0, 1) is None
```

- [ ] **Step 2: Run → ImportError.**
- [ ] **Step 3: Implement** the module (docstring = the formula block from this plan's header + the ME2/TE4 base note + copy-cost-exclusion note). Verify decryptor dogma attribute IDs against the SDE (`ssh thunderborn-home "docker exec -i -u vigilant vigilant-app-1 python3 -"` with a read-only query over `sde_type_dogma_attributes` for group 1304 types) and hard-code the verified `DECRYPTORS` table with the real values, citing the query date in a comment.
- [ ] **Step 4: Verify** tests + full suite.
- [ ] **Step 5: Commit**

```bash
git add app/industry/invention.py tests/test_invention_math.py
git commit -m "feat(industry): pure invention probability + expected-cost math"
```

---

### Task 3: Skill resolution + inventability data access (Sonnet)

**Goal:** Route-side helpers: which products are inventable (with their datacores/skills), and each character's relevant skill levels.

**Files:**
- Modify: `app/sde/lookup.py` (bulk inventability lookup)
- Modify: `app/routes/industry.py` (characters endpoint + skill resolution helper)
- Test: `tests/test_invention_lookup.py` (new)

**Acceptance Criteria:**
- [ ] `sde.get_invention_data(db, product_type_ids: list[int]) -> dict[int, dict]` — for each sellable T2 product id that is inventable: `{product_type_id: {"t1_blueprint_type_id", "t2_blueprint_type_id", "probability", "base_runs", "per_run_output_qty", "datacores": [{"material_type_id","quantity"}], "skill_ids": [..]}}`. Chain: product → `SDEBlueprintInfo` (T2 bp) → `SDEBlueprintInvention` (by `product_blueprint_type_id`) → materials + skills. Bulk queries only (no per-product round trips).
- [ ] `GET /industry/build-finder/characters` → `{"characters": [{"id", "name"}]}` filtered to skills scope (copy of the fitting pattern at `app/routes/fitting.py:1310`).
- [ ] `_resolve_invention_skills(char_skills: dict[int,int], skill_ids: list[int], encryption_manual: int, science_manual: int, use_character: bool) -> tuple[int, int, int, bool]` → `(E, S1, S2, missing_flag)`. Character mode: encryption = the one skill id whose SDE group is "Encryption Methods"… **simpler and robust**: with exactly 3 skill ids, ESI levels sorted — but E vs S matters for /40 vs /30. Distinguish by name: encryption skills' type names end with "Encryption Methods" (`sde.type_ids_to_names`). Manual mode: `(encryption_manual, science_manual, science_manual, False)`.
- [ ] Tests: temp-DB fixture rows exercise `get_invention_data` (inventable product resolves fully; non-inventable absent) and `_resolve_invention_skills` (character mode with a missing science → level 0 + flag; encryption identified by name).

**Verify:** `.venv/bin/python -m pytest tests/test_invention_lookup.py -q` → pass.

**Steps:**

- [ ] **Step 1:** Failing tests (temp-DB pattern from `tests/test_networth.py`; seed `SDEBlueprintInfo` for T2 bp 11373→product 11371 qty 1, `SDEBlueprintInvention` 687→11373 prob 0.3 runs 1, materials, skills, and `SDEType` names for the skill ids with one "* Encryption Methods").
- [ ] **Step 2:** Run → fail.
- [ ] **Step 3:** Implement both helpers + endpoint (endpoint is 12 lines — mirror the fitting one, same `_SKILLS_SCOPE` string local to industry.py).
- [ ] **Step 4:** Verify + full suite.
- [ ] **Step 5: Commit**

```bash
git add app/sde/lookup.py app/routes/industry.py tests/test_invention_lookup.py
git commit -m "feat(industry): inventability lookup + character skill resolution"
```

---

### Task 4: Build Finder integration + UI (Sonnet, math review by Fable)

**Goal:** Inventable rows rank with invention overhead included; character/skill/decryptor controls on the page.

**Files:**
- Modify: `app/industry/build_finder.py` (pure ranking accepts invention inputs)
- Modify: `app/routes/industry.py` (`build_finder_results` params + composition)
- Modify: `app/templates/build_finder.html` + `app/templates/partials/build_finder_results.html`
- Test: extend `tests/test_build_finder.py` (or create if absent)

**Acceptance Criteria:**
- [ ] `rank_builds` gains optional `invention: dict[int, dict] | None` (keyed by product_type_id: `{"overhead_per_unit": float | None, "invented_me": int, "skill_missing": bool}` — overhead precomputed by the route via the Task 2 module). For products present in the dict: build cost recomputed at `invented_me` (NOT the page ME), overhead added to cost/unit; `None` overhead → row unpriced (excluded-and-counted convention). Row dict gains `invention_overhead`, `invented_me`, `skill_missing`.
- [ ] Route params: `character_id: int = 0`, `encryption: int = Query(4, ge=0, le=5)`, `science: int = Query(4, ge=0, le=5)`, `decryptor: str = Query("none")`. Composition: `get_invention_data` for the group's products → per-product P (character skills via `_resolve_invention_skills` when `character_id` set and owned) → `attempt_cost`/`invented_bpc`/`invention_overhead_per_unit` → the dict for `rank_builds`.
- [ ] UI: character dropdown (loads from `/industry/build-finder/characters`, htmx-friendly plain fetch matching the page's existing JS style), encryption/science selects (disabled when a character is chosen), decryptor chip row (radio-style chips, "No decryptor" default). Result rows for inventable items show cost as `build + invention` with a muted ` (+N inv)` suffix and an ⚠ marker when `skill_missing`. Footnote: formula, ME2/TE4 base, copy-cost exclusion, all-assumptions line.
- [ ] Empty invention tables (pre-reimport) → controls render, rankings unchanged, footnote shows "invention data not yet imported".
- [ ] Pure-rank tests: inventable row's cost includes overhead at invented ME; None overhead excludes; non-inventable rows byte-identical to before.

**Verify:** `.venv/bin/python -m pytest tests/ -q` → all pass.

**Steps:**

- [ ] **Step 1:** Failing pure tests for the `rank_builds` extension (fixture products, one inventable with `invented_me=1`, hand-computed expected cost/unit: `calc_material`-adjusted at ME 1 + overhead).
- [ ] **Step 2:** Run → fail.
- [ ] **Step 3:** Implement pure extension, then route composition, then templates. Keep the invention block in `rank_builds` under ~30 lines — the heavy lifting stays in the route + Task 2 module.
- [ ] **Step 4:** Full suite + Jinja parse check on both templates.
- [ ] **Step 5: Commit**

```bash
git add app/industry/build_finder.py app/routes/industry.py app/templates/build_finder.html app/templates/partials/build_finder_results.html tests/test_build_finder.py
git commit -m "feat(industry): invention expected-cost in Build Finder rankings"
```

---

### Task 5: Deploy + SDE reimport runbook (coordinator/Fable — not a subagent task)

- [ ] Pre-deploy checklist → push → `ssh thunderborn-home "/opt/vigilant/scripts/deploy.sh"` (new tables auto-create).
- [ ] Trigger reimport per CLAUDE.md SDE Reload: `docker exec` delete the `sde_meta` `last_updated` row → `docker compose restart app` → watch `docker logs` for "Imported N invention" lines. Reimport runs in startup background; expect minutes on the HDD. **Do NOT stop the app for this** (see the 2026-07-10 ANALYZE lesson).
- [ ] Verify: `SELECT count(*) FROM sde_blueprint_invention` > 3000; `/industry/build-finder` with a T2 group shows `(+N inv)` suffixes; character dropdown populates; footnote correct.
