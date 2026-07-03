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
- [x]A [ ]V partials/dashboard_big_battle_banner.html — clean
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

## Batch B — Industry / Assets / Corp (not analyzed this pass)

- [ ]A [ ]V appraisal.html
- [ ]A [ ]V assets.html `[style-block]`
- [ ]A [ ]V blueprints.html
- [ ]A [ ]V compression.html
- [ ]A [ ]V corp_contracts.html
- [ ]A [ ]V corp_inventory.html
- [ ]A [ ]V corporations.html `[style-block]`
- [ ]A [ ]V hauling.html
- [ ]A [ ]V industry_jobs.html `[style-block]`
- [ ]A [ ]V industry.html
- [ ]A [ ]V journal.html
- [ ]A [ ]V mining_ledger.html `[style-block]`
- [ ]A [ ]V mining.html
- [ ]A [ ]V partials/assets_results.html
- [ ]A [ ]V partials/corp_contract_items.html
- [ ]A [ ]V partials/corp_inventory_items.html
- [ ]A [ ]V partials/corp_detail.html
- [ ]A [ ]V partials/appraisal_results.html
- [ ]A [ ]V partials/compression_results.html
- [ ]A [ ]V partials/hauling_resolved.html
- [ ]A [ ]V partials/mining_ledger_corp.html
- [ ]A [ ]V partials/mining_ledger_data.html
- [ ]A [ ]V partials/calc_results.html
- [ ]A [ ]V partials/corp_inventory_scan.html
- [ ]A [ ]V partials/component_panel.html
- [ ]A [ ]V partials/shopping_list.html

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

## Flagged for the user's visual pass (Batch A)

- **status.html / status_data.html** — heaviest Tailwind-eve usage in the app (13 + 98 utility-class hits). These are flat, old-look panels that will visually clash next to the new `b-panel is-glass` idiom used elsewhere. Restyling to `b-*` is explicitly out of scope for this pass (visual-judgment work) but they're the top candidate.
- **intel_kills.html / intel_kills_search.html** — both carry a large, near-duplicate `.kf-*`/`kfs-*` inline stylesheet (~100+ lines each) for the kill-feed row/detail layout. Not a runtime clash (each page loads only its own `<style>` block) but worth a look during the visual pass for whether the feed/detail rows read consistently between the live feed and advanced search pages, and whether it's worth extracting to a shared `kf.css`.
- **map.html / map_wormholes.html** — full-bleed `.b-main`/`.b-footer` override (`padding:0 !important`) is intentional (star map canvas needs the full viewport) but worth an eyeball to confirm the loading spinner and nav bar still look right against the new glass nav.
