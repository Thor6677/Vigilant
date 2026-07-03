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

## Batch C — Tools (not analyzed this pass)

- [ ]A [ ]V discordtime.html `[style-block]`
- [ ]A [ ]V dscan_view.html
- [ ]A [ ]V dscan.html
- [ ]A [ ]V fitting_saved.html `[style-block]`
- [ ]A [ ]V fitting_tool.html `[style-block] [zindex:[100]]`
- [ ]A [ ]V fittings.html
- [ ]A [ ]V gatecheck.html `[style-block] [zindex:[100]]`
- [ ]A [ ]V planetary_calculator.html `[style-block] [zindex:[1000]]`
- [ ]A [ ]V planetary_chain.html `[style-block]`
- [ ]A [ ]V planetary_lookup.html `[style-block] [zindex:[1000]]`
- [ ]A [ ]V planetary.html `[style-block]`
- [ ]A [ ]V ship_mastery.html `[style-block]`
- [ ]A [ ]V structure_age.html `[style-block]`
- [ ]A [ ]V structure_timers.html `[style-block] [keyframes] [zindex:[100]]`
- [ ]A [ ]V partials/gatecheck_finder.html
- [ ]A [ ]V partials/gatecheck_route.html
- [ ]A [ ]V partials/gatecheck_wartarget.html
- [ ]A [ ]V partials/planetary_chain_node.html
- [ ]A [ ]V partials/planetary_planet_detail.html
- [ ]A [ ]V partials/ship_mastery_check.html
- [ ]A [ ]V partials/fitting_search_results.html
- [ ]A [ ]V partials/fitting_stats.html
- [ ]A [ ]V partials/fitting_info.html
- [ ]A [ ]V partials/planetary_lookup_system.html `[style-block]`

## Batch D — Everything else (not analyzed this pass)

- [ ]A [ ]V alliance_detail.html `[style-block]`
- [ ]A [ ]V skill_plan_detail.html `[zindex:[100]]`
- [ ]A [ ]V skill_plan_shared.html
- [ ]A [ ]V skill_plans.html
- [ ]A [ ]V skills.html
- [ ]A [ ]V tool_landing.html `[style-block]`
- [ ]A [ ]V tools_activity.html `[style-block]`
- [ ]A [ ]V tools_image_view.html
- [ ]A [ ]V tools_images.html
- [ ]A [ ]V trending.html `[style-block]`
- [ ]A [ ]V wormhole_effects.html `[style-block]`
- [ ]A [ ]V wormhole_system.html `[style-block]`
- [ ]A [ ]V wormhole_type_page.html
- [ ]A [ ]V wormhole_types.html `[style-block]`
- [ ]A [ ]V wormholes.html `[style-block]`
- [ ]A [ ]V partials/skill_plan_gap.html
- [ ]A [ ]V partials/wormhole_kills.html
- [ ]A [ ]V partials/wormhole_system_list.html
- [ ]A [ ]V partials/wormhole_type_detail.html
- [ ]A [ ]V partials/live_pcu_tile.html
- [ ]A [ ]V partials/remap_results.html

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
