# Phase 2 — Command Palette & Cross-links Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers-extended-cc:subagent-driven-development.

**Goal:** Ctrl+K universal search (pages + characters + systems + items) with client-side recents/pins, plus an `entity_links` macro that cross-links every character/system/item mention to its related pages.

**Architecture:** New route module `app/routes/palette.py` (`templates` instance MUST be named `templates` — main.py's sys.modules loop pushes globals by that name) serving `/nav/palette?q=` as an htmx partial. A `<dialog id="cmdk">` in base.html (direct child of `<body>` — backdrop-filter gotcha) opened by Cmd/Ctrl+K inline JS (canonical fns in base.html — partial-override gotcha). Recents+pins are localStorage-only, rendered client-side when the query is empty. `entity_links` is a Jinja macro in `app/templates/partials/_entity_links.html` imported where used.

**Decisions locked:**
- Pinned/Recent live INSIDE the palette (empty-query state), not the horizontal nav — research pattern "recents when query is empty"; the top nav stays clean. This absorbs roadmap item 2.2.
- Palette page results come from the nav registry (`app/nav.py`) — no second page list. Every non-page deep link must exist in the route table (extend the dead-link test pattern).
- Never auto-redirect; Escape closes; arrow keys + Enter navigate.

**Model tiering:** Task 1–2 Opus 4.8; Task 3 Sonnet; Task 4 coordinator.

Baseline suite: 51 passed.

---

### Task 1: Command palette (backend + frontend + recents/pins) [Opus]

**Files:** Create `app/routes/palette.py`, `app/templates/partials/palette_results.html`; modify `app/templates/base.html` (dialog + JS + include router in `app/main.py` like other routers), tests `tests/test_palette.py`.

**Backend `/nav/palette?q=`** (auth: session user_id required, else 401/empty):
- `q` empty → return pages-only partial (all registry groups/items flattened, admin items gated by session `is_admin`) — client overlays pins/recents on top.
- `q` non-empty → up to 4 result buckets, each capped:
  - **Pages** (registry: case-insensitive substring on label or group label; include group url items; cap 8)
  - **Characters** (Character table, name ILIKE %q%, user's characters only — check the model/ownership pattern used by /characters; cap 5; link `/character/{id}`)
  - **Systems** (SDE systems table — find the model, likely `SDESystem.system_name`; prefix-match first then substring, cap 8; deep link: J-space (id 31000000-31999999... use existing WH helpers if any) → `/wormholes/system/{name}`, K-space → the star map. CHECK how /map accepts a focus/search param (read app/routes/starmap.py + map.html JS); if no query param exists, add `?focus={name}` support to the /map page (small: read param, pass to template, existing map search JS consumes it — verify feasibility by reading how map search works; if that's a rabbit hole >30 lines, link k-space systems to `/intel/kills/search?...` with the system filter param the search page actually supports — read intel_kills_search.py params — and note it).
  - **Items** (SDEType, published types, name match, cap 8; deep link: `/industry/manufacturing?search={name}` IF manufacturing.html reads a search query param — check; if not, add that small support (read param → prefill the existing search input value + trigger htmx load; keep ≤20 lines) or drop the Items bucket and report why.)
- SQL: use ILIKE/`lower(...) LIKE` with LIMIT; these tables are small-to-medium (SDE types ~50k rows) — a LIKE with limit is fine; do NOT add indexes.
- Partial template: grouped lists, each result an `<a>` with `data-cmdk-item`, label + small muted context (group name / "Character" / system region / item group), first result gets `data-cmdk-selected`.

**Frontend (base.html):**
- `<dialog id="cmdk">` direct child of body: input + results container. Styling: match design system (b-panel-ish, muted borders, var(--...) colors; page-scoped CSS in base.html's existing style area or a small `<style>`).
- JS (single canonical block in base.html, CSP nonce'd): Cmd/Ctrl+K toggles (`showModal()`/`close()`), Escape closes (dialog native), input debounced htmx request — either `hx-get` attrs on the input (`hx-trigger="input changed delay:150ms"`, `hx-target="#cmdk-results"`) or fetch; prefer htmx attrs. Arrow up/down move `data-cmdk-selected` (class toggle), Enter navigates to selected href. On navigate: push `{url, label, ts}` onto localStorage `cmdk_recents` (dedupe by url, cap 8).
- Empty-query state: results partial lists pages; JS injects two client-side sections above: **Pinned** (localStorage `cmdk_pins`) and **Recent** (`cmdk_recents`). Each page row gets a small ☆ pin toggle (event delegation; clicking star toggles pin without navigating).
- htmx gotcha: global `htmx:afterSwap` handler exists in base.html — make sure palette swaps don't fight it; keyboard handler attaches once (guard `window.__cmdkInit`).

**Tests (`tests/test_palette.py`):** TestClient (pattern from test_nav_registry.py's redirect test): unauthenticated → not 200-with-results (401/redirect/empty per implementation); authenticated session (TestClient can set session via cookies? — if session-faking is awkward, test the pure result-builder functions instead: extract `_page_results(q, is_admin)`, `_bucket_*` builders as testable units and unit-test those; endpoint smoke via monkeypatched auth if feasible). Must test: page search matches label case-insensitively; admin pages excluded for non-admin; caps respected; empty q returns pages.

**Verify:** full suite; Jinja parse base.html + partial. Commit: `feat(nav): Ctrl+K command palette with universal search, pins and recents`

---

### Task 2: `entity_links` macro + first integrations [Opus]

**Files:** Create `app/templates/partials/_entity_links.html`; modify `app/templates/character_detail.html`, the killmail detail template (find it — rendered by intel_kills.py:760), `app/templates/wormhole_system.html`; tests `tests/test_entity_links.py` (macro render test via Jinja env).

**Macro:** `{% macro entity_links(kind, id, name, current=None) %}` renders a compact chip row (small muted links, `.el-chips` page-agnostic style — add the tiny CSS to the design system css or a `<style>` in the partial? NO style in partial (it repeats) — add ~10 lines to static/css/site.css). Chips per kind (skip the chip matching `current` to avoid self-links):
- `character`: Overview `/character/{id}`, Skills `/character/{id}/skills`, Journal `/character/{id}/journal`, Blueprints `/character/{id}/blueprints`, Fittings `/character/{id}/fittings`, Mining `/character/{id}/mining`, zKillboard `https://zkillboard.com/character/{id}/` (external, rel=noopener)
- `system`: Kills `/intel/kills/search?system={name}` (VERIFY the actual param name the search page uses for system filtering — read its params; if none, use the star map link only), Map (the link shape Task 1 settled for k-space), WH detail `/wormholes/system/{name}` (only if J-space — macro takes optional `jspace=False` flag), Dotlan `https://evemaps.dotlan.net/system/{name}` (external; dotlan needs underscores for spaces — replace)
- `item`: Build `/industry/manufacturing?search={name}` (only if Task 1 added/verified the param — coordinate: read what Task 1 shipped), Appraisal `/industry/appraisal`, zKillboard item page external.
- Only include chips whose targets exist (same discipline as the registry dead-link test — add macro URL patterns to a small test that checks the parameterless prefixes against the route table).
**Integrations (minimal, one chip-row each):** character_detail.html near the top (kind=character, current='overview'), killmail detail partial for victim character + system, wormhole_system.html header (kind=system, jspace=True, current='wh').

**Verify:** full suite + Jinja render test of the macro with each kind. Commit: `feat(nav): entity_links cross-link macro + first integrations`

---

### Task 3: entity_links rollout [Sonnet]

**Files:** Templates where character/system/item names render as plain text and a chip row (or converting the name into a link set via a compact inline variant `entity_links_inline`) adds value WITHOUT layout damage. Rollout list (verify each, skip any that's layout-risky): dscan results partial (characters), gatecheck results (systems), intel_watch (systems), structure_timers (systems), corp detail (characters roster if present), fittings page (ship item names → item links), skills page top (character already has breadcrumbs — only if clean). Cap the sweep at the clean wins; report skips with one-line reasons.

**Verify:** Jinja parse all touched; full suite. Commit: `feat(nav): entity_links rollout across intel/corp/industry templates`

---

### Task 4: Deploy + verify Phase 2 [coordinator]

Checklist → push → deploy → logs clean → prod checks: `/nav/palette?q=` 401 unauthenticated; palette dialog markup present in page HTML; killfeed/character pages render with chips; no JS errors visible in a quick curl of base page (manual browser check deferred to user). Sync `.tasks.json`, close-out commit.
