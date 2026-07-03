# Template Sweep Checklist — 2026-07-03

Part of Task 5 of the site-restyle rollout (`docs/superpowers/plans/2026-07-03-site-restyle-rollout.md`).
Covers all 111 templates in `app/templates/*.html` + `app/templates/partials/*.html`.

Legend:
- `[ ]A` — static analysis pass (grep for `<style>` blocks, stale CSS refs, keyframe/z-index clashes; clear-cut fixes applied)
- `[ ]V` — visual pass (user eyeballs the live page against the new look; done in later batch tasks)
- `[style-block]` — template contains an inline `<style>` block
- `[eve-classes:N]` — N usages of Tailwind `-eve-*` utility classes (still functional, may visually clash with `b-*` glass panels)
- `[keyframes]` — template defines `@keyframes`
- `[zindex:[...]]` — inline `z-index` values >= 100 found

---

## Batch A — Core (index, dashboard, intel, map, character, admin, status + their partials)

- [x]A [ ]V index.html — clean, no page-local CSS, pure `b-*`
- [x]A [ ]V dashboard.html — clean; JS-hook classes (`group-*`, `sortable-group`, `edit-mode-only`) are unstyled by design (inline `style=""` handles visuals)
- [x]A [ ]V intel.html — clean
- [x]A [ ]V intel_kills.html `[style-block] [keyframes]` — namespaced `kf-*` styles, custom `@keyframes kf-row-in` (no collision with motion.css), z-index 50/30 (below dropdown=60/modal=100). Clean, no fix needed.
- [x]A [ ]V intel_kills_search.html `[style-block] [keyframes]` — same `kf-*`/`kfs-*` pattern, duplicates most of intel_kills.html's `.kf-*` rules (own-page `<style>`, no runtime clash, but a de-dup/extract-to-site.css opportunity — noted for later, not fixed here)
- [x]A [ ]V intel_local.html — clean; `intel-*-row` classes are pure JS hooks over `.b-table-row`
- [x]A [ ]V intel_watch.html `[style-block]` — `w-*` namespaced, all `var(--token)` refs resolve. Clean.
- [x]A [ ]V intel_dscan.html — clean
- [x]A [ ]V **map.html `[style-block] [keyframes]` — FIXED**: removed duplicate `@keyframes spin` (byte-identical to the one now defined globally in `design-system/css/motion.css`). `.b-main`/`.b-footer` full-bleed overrides kept — scoped to this page's own `<style>` block, don't leak.
- [x]A [ ]V **map_wormholes.html `[style-block] [keyframes]` — FIXED**: same duplicate `@keyframes spin` removed.
- [x]A [ ]V character_detail.html `[style-block]` — page-local classes (`.range-btn`, `.overview-grid`, `.stat-row`, etc.) all custom, no vocabulary collision, all `var(--token)` refs resolve. Clean.
- [x]A [ ]V admin.html — clean, pure `b-*`
- [x]A [ ]V status.html `[eve-classes:13]` — Tailwind-eve panel, flat old-look next to new glass panels. **Flag for visual pass** — candidate for `b-*` restyle (out of scope this pass).
- [x]A [ ]V status_data.html `[eve-classes:98]` — same, heaviest Tailwind-eve usage in the whole app. **Flag for visual pass.**
- [x]A [ ]V base.html — layout shell, reviewed as foundational infra for all Batch A pages. No stray classes, notif-dropdown already uses `z-index:var(--z-dropdown)` correctly. Clean.
- [x]A [ ]V partials/dashboard_big_battle_banner.html — fixed: var(--fg)→var(--text) ×3 (caught in spec review)
- [x]A [ ]V partials/dashboard_kill_pulse.html — clean
- [x]A [ ]V partials/dashboard_combat_profile.html — clean
- [x]A [ ]V **partials/dashboard_recent_battles.html `[style-block]` — FIXED**: `.rb-sys { color:var(--fg) }` referenced an undefined custom property (`--fg` never existed, not even in pre-restyle base.html — should have been `--text`, matching sibling rules `.rb-rank`/`.rb-meta`/`.rb-stat`). Changed to `var(--text)`.
- [x]A [ ]V partials/dashboard_activity.html — clean
- [x]A [ ]V partials/intel_kills_top.html — clean
- [x]A [ ]V partials/intel_kills_feed.html — clean
- [x]A [ ]V partials/intel_kills_detail.html — clean
- [x]A [ ]V partials/intel_kills_search_results.html — clean (includes intel_kills_feed.html)
- [x]A [ ]V partials/assets_partial.html — clean
- [x]A [ ]V partials/mail_panel.html — clean
- [x]A [ ]V partials/mail_body.html — clean
- [x]A [ ]V partials/notifications_panel.html — clean
- [x]A [ ]V partials/character_kill_stats.html — clean
- [x]A [ ]V partials/admin_overview.html — clean
- [x]A [ ]V **partials/admin_users.html — FIXED**: allowlist-search autocomplete result panel used inline `z-index:100` (coincides with `--z-modal`, risks stacking clash if a modal opens while the dropdown is showing). Retokened to `z-index:var(--z-dropdown)` (60), matching the notif-dropdown convention in base.html.
- [x]A [ ]V partials/admin_esi.html `[style-block]` — `details[open] .b-archive-chevron` state-transform rule, scoped to its own inline-styled span. Clean.
- [x]A [ ]V partials/admin_scheduler.html — clean
- [x]A [ ]V partials/admin_database.html — clean
- [x]A [ ]V partials/admin_sde.html — clean
- [x]A [ ]V partials/admin_audit.html — clean
- [x]A [ ]V partials/structure_alert_banners.html — clean, `.structure-alert-banner`/`.alert-dismiss` both defined in `static/css/site.css`
- [x]A [ ]V partials/inventory_alert_banners.html — clean
- [x]A [ ]V partials/contract_alert_banners.html — clean
- [x]A [ ]V partials/timer_alert_banners.html — clean

## Batch B — Industry / Assets / Corp

- [x]A [x]V appraisal.html — clean, pure `b-*` + inline token styles
- [x]A [x]V assets.html `[style-block]` — page-local `asset-*` classes, all `var(--token)` refs resolve, no vocabulary collision. Clean.
- [x]A [x]V blueprints.html — clean; all `is-*`/`b-*` variants defined
- [x]A [x]V **compression.html — FIXED**: `<script>` block (localStorage persist/restore + `resetCompression`) was inside `{% block title %}`, rendering into `<title>` (RCDATA — never executes, pollutes tab title; Reset button + state persistence dead). Pre-existing, not a swap break. Moved into `{% block content %}` before `{% endblock %}`.
- [x]A [x]V corp_contracts.html — clean
- [x]A [x]V corp_inventory.html — clean; `scan-low`/`scan-critical` are JS hooks with inline styles
- [x]A [x]V corporations.html `[style-block]` — page-local `corp-accordion`/`scope-pip`/drag classes, all token refs resolve. Clean.
- [x]A [x]V hauling.html — clean; `ship-entry`/`ship-capacity` are JS/template hooks, inline-styled
- [x]A [x]V industry_jobs.html `[style-block]` — `ij-*` namespaced; picker panel z-index:10 sits below token scale (dropdown=60) by design; `var(--warning, #e5c07b)` has inline fallback (pre-existing, `--warning` never defined — cosmetic note). Clean.
- [x]A [x]V **industry.html — FIXED**: same dead-script-in-`{% block title %}` bug as compression.html (`saveMfgState`/`restoreMfgState`/`resetManufacturing` + `recalculate` auto-save wrapper never executed). Moved into `{% block content %}` after the main script (it wraps `recalculate`, so order matters).
- [x]A [x]V journal.html — clean
- [x]A [x]V mining_ledger.html `[style-block]` — `.ml-selection-bar` position:fixed z-index:40 verified safe (no backdrop-filter/transform ancestor in `.b-main` chain, bottom-of-viewport so no nav clash). Chart.js hardcoded hex (#c8a951/#474747/#191919) matches theme — cosmetic note. Clean.
- [x]A [x]V mining.html — clean; `fit-arrow` is a JS hook styled via `b-muted-sm`
- [x]A [x]V partials/assets_results.html — clean; `b-badge is-warn/is-ok/is-danger` all defined
- [x]A [x]V partials/corp_contract_items.html — clean
- [x]A [x]V partials/corp_inventory_items.html — clean
- [x]A [x]V partials/corp_detail.html — clean; dynamic `struct.state_class` values (`is-warn`/`is-danger`/`is-muted`/``) all defined in components.css
- [x]A [x]V partials/appraisal_results.html — clean
- [x]A [x]V partials/compression_results.html — clean
- [x]A [x]V partials/hauling_resolved.html — clean; `var(--accent-rgb,200,170,110)` has inline fallback (pre-existing, `--accent-rgb` never defined — cosmetic note)
- [x]A [x]V partials/mining_ledger_corp.html — clean; `ml-check` defined in parent page (mining_ledger.html) style block
- [x]A [x]V partials/mining_ledger_data.html — clean
- [x]A [x]V partials/calc_results.html — clean; `build-toggle-btn` is a JS hook over `b-btn`
- [x]A [x]V partials/corp_inventory_scan.html — clean
- [x]A [x]V partials/component_panel.html — clean; uses `window.fn = window.fn || ...` guard pattern correctly
- [x]A [x]V partials/shopping_list.html — clean

## Batch C — Tools

- [x]A [x]V discordtime.html `[style-block]` — clean; `dt-*` namespaced in own block, script inside content block, no z-index/keyframes
- [x]A [x]V dscan_view.html — clean, pure `b-*`
- [x]A [x]V dscan.html — clean, pure `b-*`
- [x]A [x]V fitting_saved.html `[style-block]` — clean; `sf-*` namespaced; `sf-dps-loading` is a marker span with inline styles (no CSS rule needed)
- [x]A [x]V fitting_tool.html `[style-block] [zindex:[100]]` — clean; z-index:100 is on the 3 full-screen modals (info/char-import/EFT-import) + JS charge modal — coincides exactly with `--z-modal` (100), correct for full-viewport overlays, left as-is. In-content search dropdowns already at z:20 (below nav). Mobile browser-panel overlay z:50 ties with `--z-nav`; DOM order paints it above — works, noted. `module-search`/`module-results`/`ssb-*` are JS hooks, inline-styled. No filter/transform ancestor traps (`.b-main` chain verified).
- [x]A [x]V fittings.html — clean; `fit-arrow` is a JS hook styled via `b-muted-sm`
- [x]A [x]V **gatecheck.html `[style-block] [zindex:[100]]` — FIXED**: `.gc-dropdown` (in-content autocomplete) was z-index:100 = `--z-modal`, painting over the sticky nav (z:50) on scroll. Lowered to 20, matching fitting_tool.html's in-content dropdown convention.
- [x]A [x]V **planetary_calculator.html `[style-block] [zindex:[1000]]` — FIXED**: `#calc-sys-dd` autocomplete inline z-index:1000 (above even `--z-toast`:110) → 20. Flow-chart SVG z:5 is local, fine. `pi-flow-cell/grid/lines` are JS/SVG hooks, inline-styled. Hardcoded `#161616`/`#121212`/`#222` — cosmetic note.
- [x]A [x]V planetary_chain.html `[style-block]` — clean; `chain-item` states in own block; deep-link script inside content block
- [x]A [x]V **planetary_lookup.html `[style-block] [zindex:[1000]]` — FIXED**: `#sys-dd` autocomplete inline z-index:1000 → 20 (same fix as calculator). Hardcoded `#161616`/`#222` — cosmetic note.
- [x]A [x]V planetary.html `[style-block]` — clean; `pi-row`/`pi-arrow` expand states in own block (style block inside content, valid)
- [x]A [x]V ship_mastery.html `[style-block]` — clean; `.is-hidden` defined in own block; `mastery-details` is a JS toggle hook
- [x]A [x]V structure_age.html `[style-block]` — clean; `sa-*` namespaced incl. `is-exact/is-interpolate/is-extrapolate` method badges
- [x]A [x]V **structure_timers.html `[style-block] [keyframes] [zindex:[100]]` — FIXED ×2**: (1) `.range-btn` filter chips were used but never defined anywhere (not in old base.html either — pre-existing unstyled buttons); copied the canonical rules from mining_ledger.html/character_detail.html into the page's own style block. (2) 4 typeahead dropdowns (system/region/owner/ACL) inline z-index:100 → 20. `@keyframes timer-pulse` does NOT collide with motion.css names (`vg-*`, `spin`, `pulse`) — verified. UTC text inputs intentionally not datetime-local (per gotcha), untouched.
- [x]A [x]V partials/gatecheck_finder.html — clean
- [x]A [x]V **partials/gatecheck_route.html — FIXED**: summary stats used `b-stat-value`, a class that has never existed (components.css defines `b-stat-val`; old base.html did too). Values/labels rendered as unstyled inline spans. Converted to canonical `div.b-stat-val` / `div.b-stat-label`, preserving inline color overrides.
- [x]A [x]V **partials/gatecheck_wartarget.html — FIXED**: same `b-stat-value` → `div.b-stat-val` fix.
- [x]A [x]V partials/planetary_chain_node.html — clean; `piLoadNode` uses `typeof === 'undefined'` guard (htmx redefine gotcha respected)
- [x]A [x]V partials/planetary_planet_detail.html — clean
- [x]A [x]V partials/ship_mastery_check.html — clean
- [x]A [x]V partials/fitting_search_results.html — clean; calls parent-page functions (selectShip/addModule/addDrone), doesn't redefine them
- [x]A [x]V partials/fitting_stats.html — clean; `fr-val`/`def-hp-val`/`def-rep-val` are JS toggle hooks with inline styles
- [x]A [x]V partials/fitting_info.html — clean
- [x]A [x]V partials/planetary_lookup_system.html `[style-block]` — clean; `pi-conn-lines` SVG hook at local z:5; parent page re-parents injected scripts via `injectHtml`

## Batch D — Everything else

- [x]A [ ]V alliance_detail.html `[style-block]` — clean; page-local `b-change-row`/`b-change-dir.gain/.loss` in own block (no collision with components.css), JS template literals verified against own rules
- [x]A [ ]V **skill_plan_detail.html `[zindex:[100]]` — FIXED**: `#skill-results` + `#ship-results` in-content autocomplete dropdowns inline z-index:100 (= `--z-modal`) → 20, matching the batch C convention. `skill-row` is a pure JS drag/drop hook riding on `b-table-row` (unstyled by design). Script inside content block, no keyframes, no fixed/sticky.
- [x]A [ ]V skill_plan_shared.html — clean, pure `b-*` + inline token styles
- [x]A [ ]V skill_plans.html — clean; scope/target pickers inline-styled, script in content block
- [x]A [ ]V skills.html — clean; `remap-slider` is a marker class on range inputs (visuals via inline `accent-color`, never had a CSS rule — not broken)
- [x]A [ ]V tool_landing.html `[style-block]` — clean; `tl-*` namespaced in own head-block style, all token refs resolve
- [x]A [ ]V tools_activity.html `[style-block]` — clean; `ta-*` namespaced in head block; `ta-delta-btn` is a JS hook over `b-btn`; Chart.js hardcoded hex (#7a7a7a greys, rgba series colors) matches theme — cosmetic note. All chart scripts inside content block.
- [x]A [ ]V tools_image_view.html — clean; `b-row`/`b-row-label`/`b-row-val`/`b-eyebrow`/`is-muted` all defined in components.css
- [x]A [ ]V tools_images.html — clean
- [x]A [ ]V trending.html `[style-block]` — clean; page-local `b-trending-*` (+`.neg` state) in own block, JS template literals verified
- [x]A [ ]V wormhole_effects.html `[style-block]` — clean; `we-*` namespaced in head block
- [x]A [ ]V wormhole_system.html `[style-block]` — clean; `ws-*` namespaced in head block; SVG diagram inline-attr styled; kill panel lazy-loads via htmx
- [x]A [ ]V wormhole_type_page.html — clean; pure inline styles + includes wormhole_type_detail partial
- [x]A [ ]V wormhole_types.html `[style-block]` — clean; `wm-*`/`wh-tab*` in head block. `ws-section-title` on the Connection Matrix heading is defined nowhere on this page BUT its full rule set is duplicated inline on the same element — renders correctly, dead class name only (cosmetic note, not fixed).
- [x]A [ ]V wormholes.html `[style-block]` — clean; `wf-*`/`wh-tab*` in head block, `is-on` state defined in own block, `htmx-indicator` in site.css
- [x]A [ ]V partials/skill_plan_gap.html — clean, pure `b-*` + inline styles
- [x]A [ ]V partials/wormhole_kills.html — clean; `b-hover-row` defined in site.css; heatmap cells fully inline-styled
- [x]A [ ]V partials/wormhole_system_list.html — clean style-wise; `b-hover-surface` in site.css. NOTE (pre-existing, functional not CSS): pagination buttons `hx-include="#wh-search, #wh-class-filter, #wh-effect-filter"` reference two IDs that no longer exist in wormholes.html (filters became toggle buttons) — paging drops class/effect filters. Flagged, not fixed (out of sweep scope).
- [x]A [ ]V partials/wormhole_type_detail.html — clean, pure inline styles + `b-panel`
- [x]A [ ]V partials/live_pcu_tile.html — clean style-wise but **orphaned partial**: commit 600c6fe removed its seed `#live-pcu-tile` div from tools_activity.html (replaced by the LIVE window); `/api/live-pcu` endpoint still exists but nothing renders the tile. Its `ta-stat*` classes only exist in tools_activity's style block. Dead code candidate — flagged, not deleted in a style sweep.
- [x]A [ ]V partials/remap_results.html — clean, pure `b-*` + inline token styles

---

## Batch A fixes applied this pass

1. `app/templates/map.html` — deleted redundant page-local `@keyframes spin` (byte-identical to `design-system/css/motion.css` line 48, which now loads globally on every page via base.html).
2. `app/templates/map_wormholes.html` — same fix, same duplicate.
3. `app/templates/partials/dashboard_recent_battles.html` — `.rb-sys` used `color:var(--fg)`, an undefined custom property (never existed, including pre-restyle). Changed to `var(--text)` to match the convention used by sibling rules in the same block.
4. `app/templates/partials/admin_users.html` — allowlist search-results dropdown used inline `z-index:100` (same value as `--z-modal`). Retokened to `z-index:var(--z-dropdown)` to match the notif-dropdown pattern in base.html and avoid a stacking clash if a modal is opened while the dropdown is showing.

## Batch B fixes applied this pass

1. `app/templates/compression.html` — moved the localStorage persist/restore `<script>` out of `{% block title %}` (where it rendered as text inside `<title>` and never executed) into `{% block content %}`. Restores the Reset button (`data-click="resetCompression"`) and form-state persistence.
2. `app/templates/industry.html` — same bug, same fix. Script placed after the page's main script because it wraps `recalculate()`. Restores Reset button, manufacturing-state persistence, and auto-save-on-recalculate.

**Swap-orphan audit result: zero.** Every class, element/attribute selector, and CSS custom property from the old base.html inline block that Batch B templates use is present in the 4 new stylesheets (verified programmatically old-vs-new selector diff + per-template used-class check).

## Flagged for the user's visual pass (Batch B)

- **compression.html / industry.html** — browser tab titles were previously garbled by the embedded script text; now show clean titles. Verify Reset buttons + state restore actually work in the live app (they were dead code before this fix, so this is *new* behavior lighting up, not a regression risk from the restyle).
- `--warning` (industry_jobs.html) and `--accent-rgb` (hauling_resolved.html) are referenced with inline fallbacks but never defined in tokens.css — works fine, but if you want them tunable, add to tokens.css.
- mining_ledger.html Chart.js colors are hardcoded hex matching the theme (gold #c8a951, greys) — fine visually, just not token-driven.

## Flagged for the user's visual pass (Batch A)

- **status.html / status_data.html** — heaviest Tailwind-eve usage in the app (13 + 98 utility-class hits). These are flat, old-look panels that will visually clash next to the new `b-panel is-glass` idiom used elsewhere. Restyling to `b-*` is explicitly out of scope for this pass (visual-judgment work) but they're the top candidate.
- **intel_kills.html / intel_kills_search.html** — both carry a large, near-duplicate `.kf-*`/`kfs-*` inline stylesheet (~100+ lines each) for the kill-feed row/detail layout. Not a runtime clash (each page loads only its own `<style>` block) but worth a look during the visual pass for whether the feed/detail rows read consistently between the live feed and advanced search pages, and whether it's worth extracting to a shared `kf.css`.
- **map.html / map_wormholes.html** — full-bleed `.b-main`/`.b-footer` override (`padding:0 !important`) is intentional (star map canvas needs the full viewport) but worth an eyeball to confirm the loading spinner and nav bar still look right against the new glass nav.

## Batch C fixes applied this pass

1. `app/templates/partials/gatecheck_route.html` + `partials/gatecheck_wartarget.html` — summary stat strips used `span.b-stat-value`, a class that has **never** been defined (components.css and old base.html both define `b-stat-val`). Pre-existing bug, not a swap orphan: values/labels rendered as unstyled inline text. Converted to the canonical `div.b-stat-val` / `div.b-stat-label` pattern used by every other stats strip in the app, keeping the per-stat inline color overrides.
2. `app/templates/gatecheck.html` — `.gc-dropdown` autocomplete z-index:100 → 20 (was equal to `--z-modal`; in-content dropdowns should slide UNDER the sticky nav (`--z-nav`:50) on scroll — fitting_tool.html already uses 20 for the identical pattern).
3. `app/templates/planetary_lookup.html` — `#sys-dd` autocomplete inline z-index:1000 → 20 (was above the entire token scale, incl. `--z-toast`:110).
4. `app/templates/planetary_calculator.html` — `#calc-sys-dd` autocomplete inline z-index:1000 → 20 (same).
5. `app/templates/structure_timers.html` — (a) defined `.range-btn` / `:hover` / `.is-active` in the page's style block (used by the filter chips but never defined anywhere — pre-existing unstyled default buttons); rules copied verbatim from mining_ledger.html / character_detail.html. (b) 4 typeahead result dropdowns (system/region/owner/ACL) inline z-index:100 → 20.

**Swap-orphan audit result: zero** (same programmatic check as Batch B). Both real class breaks found (`b-stat-value`, `range-btn`) predate the restyle — they were never defined in the old base.html inline block either.

**z-index decisions (Batch C):** full-screen modals in fitting_tool.html stay at 100 (= `--z-modal`, correct for viewport-covering overlays with backdrop). All in-content autocomplete dropdowns normalized to 20 (< `--z-nav`:50). `@keyframes timer-pulse` (structure_timers.html) verified non-colliding with motion.css (`vg-*`, `spin`, `pulse`).

## Flagged for the user's visual pass (Batch C)

- **gatecheck.html Route Checker / War Targets tabs** — the summary stat strips now actually render in the design-system style (big 300-weight value over uppercase label) instead of unstyled inline text. Worth an eyeball — this is dormant styling lighting up, not a regression.
- **structure-timers filter chips** (All / Hostile / Friendly / Critical) — now styled like the range chips on character detail / mining ledger instead of default browser buttons. Same "new behavior lighting up" caveat.
- **fitting_tool.html mobile module-browser overlay** sits at z:50, tying with the nav (`--z-nav`:50); DOM order paints it above so it works, but if the nav ever moves later in the DOM it would flip. Left as-is.
- planetary lookup/calculator autocomplete panels use hardcoded `#161616` bg + `#222` hover (vs `var(--surface)` elsewhere) — cosmetic only, not fixed.
- status of `#121212` header rows in planetary_calculator tables — hardcoded but matches theme; cosmetic note.

## Batch D fixes applied this pass

1. `app/templates/skill_plan_detail.html` — `#skill-results` and `#ship-results` in-content autocomplete dropdowns inline z-index:100 → 20. Same value as `--z-modal`; they'd paint over the sticky nav (z:50) on scroll. These are dropdowns, not modals — normalized to the batch C in-content dropdown convention (20).

**Swap-orphan audit result: zero** (same programmatic old-vs-new selector check as batches B/C). No class used by any batch D template exists only in the old base.html inline block. No scripts inside `{% block title %}`. No `@keyframes` at all in batch D (no motion.css collision possible). No position:fixed/sticky, no backdrop-filter ancestor traps.

**Completeness check:** all 111 templates in `app/templates/` + `partials/` are covered by exactly one checklist line and every line is now `[x]A`. Zero stragglers (verified by diffing the file inventory against the checklist entries).

## Flagged for the user's visual pass (Batch D)

- **skill_plan_detail.html Add Skill / Add from Ship dropdowns** — now slide under the sticky nav when scrolling instead of painting over it. Behavior change is the fix working; eyeball that results are still fully visible in normal (unscrolled) use.
- **partials/live_pcu_tile.html is dead code** — nothing seeds `#live-pcu-tile` since commit 600c6fe (LIVE window replaced it). If the tile isn't coming back, delete the partial + the `/api/live-pcu` route in `app/routes/dashboard.py` (~line 851) in a cleanup pass.
- **wormhole_system_list.html pagination loses filters** (pre-existing, functional): page 2+ requests only include `#wh-search`; the `#wh-class-filter`/`#wh-effect-filter` IDs it tries to include were removed when wormholes.html moved to toggle-button filters. Fixing means threading current filter state into the pagination links — out of scope for the style sweep.
- wormhole_types.html carries a dead `ws-section-title` class name (styles duplicated inline on the same element) — cosmetic only.
- tools_activity.html Chart.js colors are hardcoded (matches theme) — same note as mining_ledger in batch B.

