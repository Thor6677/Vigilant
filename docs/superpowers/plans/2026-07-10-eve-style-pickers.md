# EVE-Style Tree Pickers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Execution model:** Task 1 → **Opus** (traversal correctness). Tasks 2–3 → **Sonnet** (integration). Task 4 → **Haiku** (mechanical UI, precisely specified). **Fable reviews each task before the next.** Task 5 = coordinator deploy.

**Goal:** In-game-market-style tree pickers replace the flat selects on `/industry/build-finder` (market-group tree + search) and `/market/lp` (faction → corp tree + search).

**Architecture:** Spec `docs/superpowers/specs/2026-07-10-eve-style-pickers-design.md`. Data already present: `SDEMarketGroup(market_group_id, parent_group_id, market_group_name)`; helpers `get_market_group_children`, `get_market_group_items`, `get_market_group_path` in `app/sde/lookup.py`; fitting's browse endpoints (`app/routes/fitting.py:1201`) are the fragment idiom. Existing form-serialization htmx plumbing on both pages carries new params — never mutate `hx-get`.

**Tech Stack:** FastAPI + SQLAlchemy async, Jinja2 fragments over htmx, pytest.

---

### Task 1: Subtree products resolver + tree-search lookup (Opus)

**Goal:** Pick any market-group node → all buildable products in its subtree, shaped exactly like `get_group_buildables`; plus a name-search over groups with path labels.

**Files:**
- Modify: `app/sde/lookup.py` (new helpers near `get_group_buildables`, ~line 1260)
- Test: `tests/test_market_group_subtree.py` (new)

**Acceptance Criteria:**
- [ ] `get_market_group_descendants(db, market_group_id) -> set[int]` — the node id + every descendant id. One query loads all `(market_group_id, parent_group_id)` pairs; BFS in Python (the table is ~2.5k rows; NO recursive SQL).
- [ ] `get_market_group_subtree_products(db, market_group_id, cap) -> tuple[int, list[dict]]` — `(total_count, products[:cap])`; each product dict has the SAME keys `get_group_buildables` returns (`product_type_id`, `product_name`, `blueprint_type_id`, `product_quantity`, `materials`) — READ `get_group_buildables`' implementation first and mirror its product/materials assembly (published types whose `market_group_id` ∈ descendant set and that have an `SDEBlueprintInfo` row; materials from `SDEBlueprintMaterial` activity 1).
- [ ] `search_market_groups(db, q, limit=30) -> list[dict]` — case-insensitive substring on `market_group_name`, each row `{"market_group_id", "market_group_name", "path"}` where path comes from the existing `get_market_group_path` helper (read its signature first).
- [ ] Tests (temp-DB pattern from `tests/test_invention_lookup.py`): 3-level fixture tree (root 10 → children 11, 12 → grandchild 13 under 11) with buildable products at depths 11 and 13 and a non-buildable type at 12; assert descendants(10) == {10,11,12,13}; subtree_products(10) finds both products, subtree_products(11) finds both (13 is under 11), subtree_products(12) is (0, []); cap=1 returns total 2 with 1 row; search "frig" matches case-insensitively and carries a " > "-joined path.

**Verify:** `.venv/bin/python -m pytest tests/test_market_group_subtree.py -q` → all pass; full suite green.

**Steps:**
- [ ] Step 1: failing tests per the criteria above (write them fully; fixture-seed `SDEMarketGroup`, `SDEType` (with `market_group_id` + `published`), `SDEBlueprintInfo`, `SDEBlueprintMaterial`).
- [ ] Step 2: red run.
- [ ] Step 3: implement the three helpers.
- [ ] Step 4: green + full suite.
- [ ] Step 5: commit — `git add app/sde/lookup.py tests/test_market_group_subtree.py && git commit -m "feat(sde): market-group subtree products + group search

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"`

---

### Task 2: Build Finder tree UI + endpoint swap (Sonnet)

**Goal:** The page's group `<select>` becomes the expandable tree + search; results keyed by `market_group_id`.

**Files:**
- Modify: `app/routes/industry.py` (tree + search endpoints; `build_finder_results` param swap; page route drops `get_buildable_groups`)
- Modify: `app/templates/build_finder.html`
- Create: `app/templates/partials/build_finder_tree.html` (node-list fragment, used by both tree levels and search results)
- Modify: `tests/test_build_finder.py` (route tests: seed SDEMarketGroup rows, use `market_group_id`)

**Acceptance Criteria:**
- [ ] `GET /industry/build-finder/tree?parent=0` → fragment of root groups (`parent_group_id IS NULL`), each node: expand arrow (`hx-get` next level into a nested container, `hx-swap="outerHTML"` on a per-node child slot — follow fitting's browse fragment idiom at `app/routes/fitting.py:1201` + its template) when it has children, plus a select button setting the hidden input. `parent=<id>` → that node's children. Auth-gated (401 empty like other partials).
- [ ] `GET /industry/build-finder/tree/search?q=` (min 2 chars) → same fragment shape from `search_market_groups`, nodes labeled with their `path`.
- [ ] Node select (plain JS helper in build_finder.html): sets `#bf-market-group` hidden input (inside the existing htmx form) + a visible `#bf-selected-path` label, then `htmx.trigger` the existing form submit mechanism (find how ME/structure changes trigger results today and reuse it exactly).
- [ ] `build_finder_results`: `market_group_id: int = Query(0)` replaces `group_id`; composition calls `sde.get_market_group_subtree_products(db, market_group_id, cap=BUILD_FINDER_CAP)`; unknown/0 → existing "pick a group" empty state. Delete `get_buildable_groups` usage from the page route; delete the lookup function too if `grep -rn get_buildable_groups app/` shows no other caller.
- [ ] Search input debounced 300ms (follow the page's existing JS style), results replace the tree container; clearing the box restores the root tree.
- [ ] Tests updated: seed a small market-group tree; results test passes `market_group_id`; add a tree-endpoint fragment test + a search test; invention route tests keep passing.

**Verify:** `.venv/bin/python -m pytest tests/ -q` all green; Jinja `Environment().parse` on both templates; `ast.parse` on industry.py.

**Steps:** red tests for the two new endpoints + updated results param → implement endpoints + partial + template swap → green full suite → commit `git add -A app/routes/industry.py app/templates/build_finder.html app/templates/partials/build_finder_tree.html app/sde/lookup.py tests/test_build_finder.py && git commit -m "feat(industry): EVE-style market-group tree picker for Build Finder

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"`

---

### Task 3: LP faction mapping + tree endpoint (Sonnet)

**Goal:** Corp roster grouped by faction, served as one tree fragment.

**Files:**
- Modify: `app/market/lp.py` (faction map cache)
- Modify: `app/routes/market.py` (tree endpoint; page passes tree instead of flat corps)
- Test: `tests/test_lp_factions.py` (new)

**Acceptance Criteria:**
- [ ] `get_corps_by_faction() -> list[dict]` in lp.py: `[{"faction_name": str, "corps": [{"corporation_id", "name"}, ...]}, ...]` — majors (Amarr Empire, Caldari State, Gallente Federation, Minmatar Republic) first alphabetically, then remaining factions alphabetically, "Other" last. Backed by a module-scope `_corp_faction_map` cache built once: `GET /universe/factions/` (names) + per-corp `GET /corporations/{id}/` (public; `faction_id` optional → "Other") using the ESI bulk pattern — semaphore 3, batches of 10, `await asyncio.sleep(1)` between batches — single-flight lock, failure NOT cached (mirror `get_npc_corps`' discipline in the same file; read it first).
- [ ] On faction-fetch failure: return the roster all under "Other" with `{"degraded": True}` in the payload so the template can show a muted retry note; the cache stays unpopulated (next call retries).
- [ ] `GET /market/lp/corps-tree` in market.py: auth-gated, renders `partials/lp_corp_tree.html` (Task 4 creates it — for THIS task return the context through a minimal placeholder template you create with just the data structure iterated as a flat list; Task 4 restyles it) with the faction list.
- [ ] Page route (`/market/lp`): keeps working; passes nothing new (the tree loads via htmx `hx-get="/market/lp/corps-tree" hx-trigger="load"` — wire the container div into `market_lp.html`, replacing the flat `<select>` block; the existing offers-load mechanism keyed on `corporation_id` must keep working — read how the select currently triggers `/market/lp/offers` and preserve that contract via a hidden input set by corp clicks (Task 4 wires the clicks; leave the hidden input + htmx plumbing in place now)).
- [ ] Tests: mock the ESI calls (monkeypatch the fetch helpers): grouping order (majors first, Other last), corp without faction_id → Other, fetch failure → degraded payload + retry-on-next-call, tree endpoint auth + fragment renders.

**Verify:** `.venv/bin/python -m pytest tests/test_lp_factions.py tests/ -q` all green.

**Steps:** red → implement → green → commit `git add app/market/lp.py app/routes/market.py app/templates/market_lp.html app/templates/partials/lp_corp_tree.html tests/test_lp_factions.py && git commit -m "feat(market): faction-grouped LP corp tree data + endpoint

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"`

---

### Task 4: LP tree template + client-side filter (Haiku)

**Goal:** The placeholder tree fragment becomes the collapsible faction → corp browser with a search filter.

**Files:**
- Modify: `app/templates/partials/lp_corp_tree.html`
- Modify: `app/templates/market_lp.html` (search input + selection JS)

**Acceptance Criteria (mechanical — follow exactly):**
- [ ] `lp_corp_tree.html` renders, for each faction: a header row `<button type="button" class="lpt-fac" data-fac="{{ loop.index }}">▸ {{ f.faction_name }} <span class="b-muted-sm">({{ f.corps|length }})</span></button>` followed by `<div class="lpt-corps" data-fac="{{ loop.index }}" style="display:none;">` containing one `<button type="button" class="lpt-corp" data-corp-id="{{ c.corporation_id }}">{{ c.name }}</button>` per corp `</div>`. When `degraded` is true, render at top: `<div class="b-muted-sm" style="font-style:italic;">Faction data unavailable — showing all corporations under Other; reload to retry.</div>`.
- [ ] Styles: add a `<style nonce="{{ request.state.csp_nonce }}">` block INSIDE the fragment (htmx-swapped fragments keep inline styles) with: `.lpt-fac { display:block; width:100%; text-align:left; background:none; border:none; color:var(--text); font:inherit; font-size:11px; padding:4px 6px; cursor:pointer; border-bottom:1px solid var(--border); } .lpt-fac:hover { color:var(--accent); } .lpt-corp { display:block; width:100%; text-align:left; background:none; border:none; color:var(--muted); font:inherit; font-size:11px; padding:3px 6px 3px 22px; cursor:pointer; } .lpt-corp:hover, .lpt-corp.is-selected { color:var(--accent); }`.
- [ ] In `market_lp.html`: above the tree container add `<input type="text" id="lp-corp-filter" placeholder="Filter corporations…" autocomplete="off" style="width:100%;padding:0.35rem 0.5rem;font-size:11px;background:var(--bg);border:1px solid var(--border);color:var(--text);font-family:inherit;margin-bottom:0.4rem;">`.
- [ ] JS (inside `{% block content %}`, following the page's existing script style; use event delegation on the tree CONTAINER because the fragment is htmx-swapped in after load):
  - Click `.lpt-fac` → toggle its matching `.lpt-corps[data-fac]` display and swap the leading `▸`/`▾` character.
  - Click `.lpt-corp` → set the existing hidden `corporation_id` input, add `is-selected` (removing it from others), and trigger the existing offers load exactly as Task 3 wired it (read the current mechanism in market_lp.html and reuse — do not invent a new one).
  - Input on `#lp-corp-filter`: lowercase substring against each `.lpt-corp` text; non-matching corps `display:none`; factions with zero visible corps hide entirely; factions with matches force their corps container open; empty query restores collapsed-all state.
- [ ] No behavior change to the offers table itself.

**Verify:** `.venv/bin/python -m pytest tests/ -q` all green (Task 3's fragment test must still pass — keep the data-corp-id attributes it asserts); `python3 -c "from jinja2 import Environment; Environment().parse(open('app/templates/partials/lp_corp_tree.html').read())"` and same for market_lp.html.

**Steps:** implement (no new tests required beyond keeping Task 3's green — this is template/JS mechanics) → verify → commit `git add app/templates/partials/lp_corp_tree.html app/templates/market_lp.html && git commit -m "feat(market): collapsible faction tree + filter for LP corp picker

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"`

---

### Task 5: Deploy + live verify (coordinator/Fable)

- [ ] Full suite green → push → deploy.sh → healthz + both pages 200.
- [ ] Live: tree expands (Ships → subgroup), selecting a branch ranks products ("showing N of M" when capped), search shows paths; LP tree groups by faction (first request pays the one-time ~270-corp ESI pass — watch logs), corp click loads offers, filter works.
