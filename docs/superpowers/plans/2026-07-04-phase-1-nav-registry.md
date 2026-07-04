# Phase 1 — Navigation Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development to implement this plan task-by-task.

**Goal:** One Python nav registry drives the desktop nav, mobile menu, footer, and landing card grids — killing the 4-way hand-maintained duplication that produced orphan pages, contradictory grouping, and naming drift. Orphans get entries; legacy `/dscan` routes redirect; naming unifies.

**Architecture:** New module `app/nav.py` holds `NAV_GROUPS` (list of group dicts with items carrying label/url/match-rules/desc/features/flags) plus pure helpers `item_active(item, path)` / `group_active(group, path)`. `main.py` registers `nav_groups` + helpers as Jinja globals (must go through the existing sys.modules loop that pushes globals to ALL ~31 Jinja2Templates instances — see memory note "Jinja Templates Per Module"). `base.html` renders all three chrome surfaces from it; `landings.py` builds its card grids from it.

**Tech Stack:** FastAPI, Jinja2, pytest (baseline: 31 passed).

**Model tiering:** Tasks 1–2 Opus 4.8; Tasks 3–4 Sonnet; Task 5 coordinator.

**Regrouping/renaming decisions (locked in, baked into registry data):**
- Groups (left→right): **Dashboard** (dropdown: Overview `/dashboard`, Characters `/characters`), **Corporations**, **Skill Plans**, **Industry** (unchanged 8 items), **Intel** (Overview, Kill Feed, **Kill Search** `/intel/kills/search` [NEW], D-Scan / Local, **Watchlist** [renamed from "Local Watch"], Gate Check), **Map** [NEW GROUP, parent url `/map`] (Star Map `/map`, Wormhole Map `/map/wormholes` [NEW], Trending `/trending` [NEW], divider, Wormhole Systems, Wormhole Types, System Effects, Wanderer external link [moved from Tools]), **Tools** (Overview, Activity, Asset Search, Structure Timers, Image Host, Ship Fitting, Saved Fits, Discord Time, **Structure Age** [moved from Intel — resolves the Intel-vs-Tools contradiction in favor of its /tools/ URL + landing-card placement]), **Admin** (admin-only; dropdown: Console `/admin`, Status `/status` [de-orphaned]).
- Footer: one link per group (group label → group url), same admin gating. This replaces the current inconsistent footer set.
- Active-state rules become data: each item carries `match` (list of `("exact"|"prefix", path)` tuples); group is active iff any item matches OR extra group-level `match` entries (e.g. Dashboard group also matches `/character/` prefix; Admin group also matches `/status`; Intel keeps `/dscan` prefix until the redirect ships; Tools keeps `/i/` for image pages).

---

### Task 1: `app/nav.py` registry + helpers + tests [Opus]

**Files:** Create `app/nav.py`, `tests/test_nav_registry.py`.

**Registry item shape** (plain dicts, no classes):
```python
{"label": "Kill Feed", "url": "/intel/kills", "match": [("prefix", "/intel/kills")],
 "desc": "...", "features": ["..."],      # optional; used by landing grids
 "admin": False, "external": False, "divider_before": False,
 "in_dropdown": True, "in_landing": True}  # visibility flags, default True
```
Group shape: `{"label": "Intel", "url": "/intel", "match": [...extra rules...], "items": [...], "admin": False, "landing": True}`. Groups without dropdowns (Corporations, Skill Plans) have `items: []`.

**Content:** Port every current nav item + the regrouping decisions above. Port `desc`/`features` for existing landing-card tools VERBATIM from `landings.py` (INDUSTRY_TOOLS/INTEL_TOOLS/TOOLS_TOOLS). Write desc+features for the NEW landing entries: Kill Feed, Kill Search, Structure Age (Tools grid — it already has a card there; reuse it), Star Map group items get `in_landing: False` (no Map landing page exists — parent url is the map itself), Characters/Status get `in_landing: False`.

**Helpers:** `item_active(item, path)` and `group_active(group, path)` — pure functions over the match tuples. Longest-wins is NOT needed; replicate current semantics (any match → active).

**Match-rule fidelity:** replicate the CURRENT active expressions from base.html:26-72 as data, including: Dashboard exact `/dashboard` + prefix `/character/`; D-Scan matches `/intel/dscan` + `/dscan`; Star Map exact `/map` (Wormhole Map exact `/map/wormholes` now its own item); Image Host prefix `/tools/images` + `/i/`; Saved Fits prefix beats Ship Fitting exact (order items so Saved Fits is checked as its own item; Ship Fitting uses exact `/tools/fitting`).

**Tests (the permanent value):**
1. **No dead links:** import `app.main`'s FastAPI `app`, collect all route paths; for every internal registry URL assert it matches a registered route (exact match or a route path equal to it). External/`#` entries skipped. This makes future orphan/dead-link drift a test failure.
2. Helper unit tests: exact vs prefix, group-level extra matches, admin flag presence.
3. Uniqueness: no duplicate URLs across items; labels unique within a group.

**Verify:** `python3 -m pytest tests/test_nav_registry.py -v` then full suite (31 + new). Commit: `feat(nav): single-source nav registry with active-state rules and dead-link test`

---

### Task 2: Render chrome from the registry [Opus]

**Files:** Modify `app/templates/base.html` (nav block lines ~22-72, mobile menu ~146-182, footer ~212-223 — NOT the notification widget ~73-134, hamburger, alert banners, or scripts), `app/main.py` (template-globals wiring).

**main.py:** find the existing loop that pushes template globals to all Jinja2Templates instances (sys.modules scan) and add `nav_groups` (the registry), `nav_item_active`, `nav_group_active`. Follow the pattern of whatever globals are already pushed.

**base.html:** replace the three hand-maintained surfaces with loops:
- Desktop: for each group — no items → plain `b-nav-link` with `group_active`; items → `b-nav-dropdown` with parent link + `b-nav-dropdown-menu` of items (render `divider_before` as the existing border-top div; external items get `target="_blank"`; admin-gated groups wrapped in the is_admin check). Keep the exact same classes/markup so CSS is untouched. Keep `+ Add Character`, notifications bell, Logout, hamburger exactly as-is (hardcoded).
- Mobile menu: same loop flattened with `b-mobile-sep` between groups, matching current structure (group parent rendered as "<Group> Overview" for groups with landing urls — replicate current labels like "Industry Overview" via `group.label ~ ' Overview'` only for the three landing groups; simpler: render parent item first with label from a `mobile_label` field defaulting to label).
- Footer: loop over groups (skip admin-gated when not admin) → `b-footer-link` per group.
- CRITICAL Jinja gotchas: use `item['items']` bracket access everywhere (`items` is a dict method name!). Scripts stay where they are.

**Sanity check nav width:** 8 top-level entries + bell + add-char + logout. Check `b-nav`'s CSS (static/ds/css/) breakpoint where the hamburger takes over; if the desktop row wraps at 1280px, shorten labels (e.g. "Skill Plans" stays, but confirm). Report findings; do not redesign CSS in this task.

**Tests:** extend `tests/test_nav_registry.py` with a render smoke test: build a Jinja env over app/templates, render base.html with a fake request/session (user_id + is_admin set, csp_nonce stubbed) — if wiring a fake request is impractical, at minimum `e.parse()` the template and assert the old hardcoded URLs are gone from the source (grep-style assertions: `'/industry/manufacturing'` appears 0 times in base.html source since it now comes from the registry).

**Verify:** full suite; `python3 -c` Jinja parse of base.html. Commit: `refactor(nav): render desktop/mobile/footer chrome from nav registry`

---

### Task 3: Landing grids from registry + naming sweep [Sonnet]

**Files:** Modify `app/routes/landings.py`, `app/templates/intel_watch.html` (title/H1 if needed), any template hardcoding "Local Watch".

- Replace INDUSTRY_TOOLS/INTEL_TOOLS/TOOLS_TOOLS constants with builders reading `app.nav`: items of that group with `in_landing` True and a `desc`, shaped into the existing card dict format (`name`←label, `url`, `desc`, `features`). Landing pages themselves (routes, template, subtitles) unchanged otherwise.
- The Intel landing will now include Kill Feed + Kill Search cards (desc/features from Task 1); Tools landing keeps Structure Age; the Watchlist card auto-renames.
- Naming: grep templates for "Local Watch" → "Watchlist" (nav already fixed via registry; check page H1/title in intel_watch.html — inventory says its title is already "Watchlist").
- **Verify:** full suite; manual grep `grep -rn "Local Watch" app/templates/ app/routes/` → 0. Commit: `refactor(nav): landing card grids read from nav registry; Watchlist naming unified`

---

### Task 4: Legacy /dscan redirects + orphan link-through [Sonnet]

**Files:** Modify `app/routes/dscan.py`; test `tests/test_nav_registry.py` or new `tests/test_dscan_redirects.py`.

- Replace the duplicate page routes `/dscan` (dscan.py:438) and `/dscan/{scan_id}` (dscan.py:555) handler bodies with 301 `RedirectResponse` to `/intel/dscan` and `/intel/{scan_id}` respectively (preserve query strings; check whether any POST/partial endpoints live under /dscan — redirect GET page routes ONLY, leave POST/API endpoints untouched and list them in the report).
- Remove the now-dead `/dscan` prefix from the Intel group's extra match rules in `app/nav.py` (redirects make it unreachable as a resting path).
- Test: TestClient GET `/dscan` → 301 Location `/intel/dscan`; `/dscan/123` → `/intel/123`. If building a TestClient against app.main is heavy (SDE init etc.), test the router in isolation via a minimal FastAPI app including dscan.router.
- **Verify:** full suite. Commit: `fix(nav): 301 legacy /dscan page routes to /intel/dscan (T-039 adjacent)`

---

### Task 5: Page-header + breadcrumb consistency sweep [Sonnet]

**Files:** Templates missing `.b-page-header` (from the audit): `intel_watch.html`, `intel_kills.html`, `intel_kills_search.html`, `wormhole_type_page.html`, and the rest of the 11 EXCEPT full-bleed canvas pages (`map.html`, `map_wormholes.html` — SKIP, a page header would break the viewport layout) and `status.html`/`status_data.html` (already done in Phase 0). Run `grep -L "b-page-header" app/templates/*.html` (excluding partials/, base, index) to get the authoritative list first.

- For kill feed/search: these have a bespoke `.kf-head` — do NOT rip it out; instead ensure it contains a `.b-page-title`-styled heading (add the class alongside, or wrap) so the pages read consistently; judgment call: minimal markup change, zero layout risk. If the layout risk feels non-trivial, report instead of forcing it.
- Breadcrumbs: ensure `{% block breadcrumbs %}` is defined on character/corp SUBpages only (char skills/journal/blueprints/fittings/mining, corp subpages) with the pattern from existing pages (e.g. `admin.html:3`); REMOVE none elsewhere (leave existing ones alone).
- **Verify:** Jinja parse every touched template; full suite; `grep -L` list shrinks to just the intentional skips. Commit: `style(nav): page-header + breadcrumb consistency sweep`

---

### Task 6: Deploy + verify Phase 1 [coordinator]

Pre-deploy checklist (ast-parse changed .py, suite green, no schema changes, no auth-file changes) → push → deploy.sh → log check → spot checks: nav renders on /dashboard (desktop + mobile menu HTML present), /intel landing shows Kill Feed/Kill Search cards, /dscan 301s, footer shows group links, no Jinja errors in logs. Update `.story` tickets if touched; sync `.tasks.json`.
