# Vigilant Site Audit & Roadmap — 2026-07-04

Research synthesis from a three-track audit: (A) full site/navigation inventory, (B) bug hunt
against documented gotcha patterns, (C) competitive research across the EVE third-party tool
ecosystem plus dashboard-IA best practices. Produces a tiered execution plan: **Opus 4.8** for
architectural/correctness-sensitive work, **Sonnet** for mechanical sweeps and pattern-following
rollouts. Fable 5 (this session) owns planning and any follow-up design docs.

---

## Part 1 — State of the site (findings)

### 1.1 Navigation

- **~70 user-facing pages; the global nav exposes ~30.** Depth is 2 levels (top item → one
  dropdown). ~40 pages are reachable only by drilling from a parent page.
- **True orphans** (no chrome-level entry point at all):
  - `/characters` (character mgmt/reorder — characters.py:210)
  - `/trending` (starmap.py:1762)
  - `/status` and `/status/data` (status.py:175/184 — only *light up* the Admin tab, no link)
  - `/intel/kills/search` (878-line page, reachable only via a link inside the kill feed)
  - `/map/wormholes` (has active-state handling in base.html:50 but **no menu item**)
- **Nav is maintained in four places by hand**: desktop nav (base.html:22–145), mobile menu
  (base.html:146–182), landings.py card grids, footer (base.html:212–223). This triplication+
  is the root cause of every observed drift:
  - Structure Age filed under **Intel** in the header (base.html:55) but **Tools** in
    landings.py (`TOOLS_TOOLS`, landings.py:234).
  - Intel dropdown and `/intel` landing page list **different tool sets** (dropdown has Kill
    Feed + Structure Age; landing has D-Scan/Gate Check/Watchlist/Star Map/Wormholes instead).
  - Footer promotes **Assets** to top level; header buries it in Tools.
  - Naming drift: nav "Local Watch" vs landing/title "Watchlist" vs URL `/intel/watch`;
    page H1 "Manufacturing Calculator" vs `<title>` "INDUSTRY".
- **Duplicate route families**: `/dscan` vs `/intel/dscan`, `/dscan/{scan_id}` vs
  `/intel/{scan_id}` (dscan.py:394/438/509/555) — two live URL families for one feature.
- **Active-state logic is a pile of special cases** — parents claim paths their dropdown never
  lists (`/i/`, `/structure-timers`, `/assets` under Tools; `/dscan`, `/wormholes` under Intel).
- **No global search / command palette.** The flat menu is the only discovery mechanism.
- **Breadcrumbs opt-in and uneven**: 34/57 templates define the block, 23 don't, with no
  pattern even within a section.

### 1.2 Page-level consistency

- 46/57 templates use the standard `.b-page-header`; **11 full pages have no page header**
  (incl. map.html, intel_watch.html, intel_kills.html, intel_kills_search.html, status.html).
  Kill Feed/Search roll a bespoke `.kf-head`.
- **status.html + status_data.html are the only two pages still on legacy Tailwind `eve-*`
  classes** — missed by the SP1/SP2 design-system rollout (candidates for T-039 scope).
- Filter bars are bespoke per tool (`.kf-chip`, `.gc-tabs`, inline-styled inputs) — no shared
  primitive. Loading/empty states are ad hoc outside the global htmx error fallback.

### 1.3 Bugs (ranked)

**HIGH — confirmed:**

- **BUG-1** `app/routes/dashboard.py:1306` — `asyncio.gather` runs multiple field fetchers on
  **one shared AsyncSession**; several (`fetch_corp_roles_data` :1113,
  `_resolve_assets_for_character` :933) write/commit on it. Two stale db-using fields at once
  → concurrent commit on one session → greenlet/`InvalidRequestError`, swallowed by the
  per-field except → **intermittent silent sync failures / stale dashboard data**. Exact
  instance of the documented async-session gotcha.
- **BUG-2** `app/routes/dashboard.py:994–1007` + `app/esi/universe.py:21–60` — structure-name
  gather shares the request session; every coroutine can `db.execute()` + `cache_structure_name`
  → `db.commit()`. Symptom: assets intermittently labeled "Unknown Structure", possibly
  poisoned session for the rest of the request. Proven in-repo fix pattern: per-coroutine
  `AsyncSessionLocal()` as in `app/routes/mining.py:284` and `app/esi/market.py:63`.

**MEDIUM:**

- **BUG-3** `app/intel/kill_queries.py:371–397` — `streaks()` scans a character's **entire
  all-time** kill history as raw rows (no time bound, Python iteration), runs on every
  character-detail load. Against the 10M+-row killmails table a prolific character makes this
  the slowest span on the page.
- **BUG-4** `app/routes/intel_kills_search.py:786–795` — the COUNT+SUM query only gets its
  `INDEXED BY` hint when a lower time bound exists; a filterless search scans the whole table.
  Needs a 5-minute EXPLAIN + timing check on the VPS, then a default/enforced time bound.
  *(Overlaps ticket T-037 "killfeed search hardening follow-ups" — fold in.)*
- **BUG-5** `app/routes/dashboard.py:2986–2988` — corp-stats `except Exception: pass` renders
  wallet/jobs/orders as 0 with zero logging. Add `logger.warning`.
- **BUG-6** `app/routes/fittings.py:176–178` — unbounded gather over `_get_ship_info` per
  distinct hull; >20 hulls violates the ESI fan-out rule. Verify cache hit rate, then add
  semaphore.

**LOW:** silent excepts in login enrichment (auth/routes.py:147–163, add debug log);
fetch()-in-IIFE triage list (dashboard.html, structure_timers.html, skill_plan_detail.html,
fitting_tool.html); TODO(ISS-015) in fitting engine (already tracked).

**Security (SECURITY_TODO.md, all medium):** VVP-2026-007 (stale `nginx/vigilant.conf` in repo
post-decoupling — delete), VVP-2026-020 (docs/nginx-sample.conf unhardened patterns),
VVP-2026-021 (`static/js/notifications.js:161` innerHTML interpolation — escape it).

**Verified clean:** Jinja script placement, dict method-name access, datetime-local, Tailwind-
in-Python, corp ESI fallbacks, killmail query hygiene elsewhere (T-040 pattern held), all other
ESI fan-outs properly throttled.

### 1.4 Feature gaps vs the ecosystem (corrected & ranked)

*(Research agent's "appraisal missing" claim was wrong — `/industry/appraisal` exists.
List below is corrected against the actual route inventory.)*

| # | Gap | Best-in-class | Value | Size |
|---|-----|---------------|-------|------|
| 1 | Market price-history charts + hub order-book browser | Adam4EVE | Very high — biggest missing pillar; foundation for #4, #6, #7 | M |
| 2 | Net-worth-over-time tracker (wallet+assets+escrow+orders) | jEveAssets | High — all data already synced; reuse T-040 daily-aggregate pattern | M |
| 3 | Discord alert relay (structure attack, fuel, skill queue, inventory) | SeAT/AA | High — browser notifications don't reach a sleeping player; webhook infra proven in thunderborn-ops | S |
| 4 | "What should I build?" profitability ranking + invention/decryptor math | Ravworks | High — turns calculator into decision tool; needs #1 | M-L |
| 5 | EVE-Scout Thera/Turnur/Zarzakh shortcuts in router + map overlay | EVE-Scout API | Med-high — big jump savings, small surface | S |
| 6 | LP store ROI calculator (ISK/LP per corp store) | Fuzzwork | Med-high for mission/FW play; SDE has offers | S-M |
| 7 | Trade & industry P&L (FIFO transaction matching) | EVE Tycoon | Very high *if* actively trading/building; complex edge cases | L |
| 8 | zKB-style entity stats from local archive (activity heatmap, solo-vs-gang, danger rating) | zKillboard | Med — differentiator: 192GB local data, no API limits; must pre-aggregate | M |
| 9 | Character-level stockpile watchlists (assets + orders + jobs netted) | jEveAssets | Med — corp thresholds prove the pattern | M |
| 10 | Fit-vs-fit comparison + damage profiles + implants | Pyfa | Med — extends existing engine; damage profiles already a tracked gap | M |

Honorable mentions: moon extraction schedule calendar, sov campaign board (ESI
`/sovereignty/campaigns/`), calendar/contacts sync, PWA manifest (tiny), EVERef public-contract
snapshot deal-finder. Skip: abyssal tracker (niche), community fit hub, SRP/fleet tools,
rebuilding wormhole mapping (Wanderer covers it — cross-link instead).

### 1.5 Navigation best-practice recommendations (research-backed)

1. **Grouped sidebar or restructured grouped nav** (~7 groups: Dashboard, Characters, Corp,
   Intel, Map/Wormholes, Industry, Tools; Admin separate). NN/g median for large IAs is 7
   top-level categories. Collapsible groups, localStorage state, active-group auto-expand.
2. **Ctrl+K command palette with universal search** — pages + characters + systems + items,
   recents when empty. Fits htmx cleanly: `<dialog>` in base.html, keyup trigger filter,
   `hx-get="/nav/palette?q=…"` with `input changed delay:150ms`. Search backends already exist
   (map search, item search, character list) — the palette is a thin aggregation endpoint.
   Cautions: keep canonical palette JS in base.html (partial-override gotcha); dialog as direct
   child of body (backdrop-filter gotcha).
3. **Pinned + Recent sections** in nav — localStorage only, zero backend. Explicit list, never
   auto-redirect.
4. **`entity_links(kind, id)` Jinja macro** — chip-row of cross-links wherever a character /
   system / item appears (char → skills/journal/blueprints/fittings; system → map/gatecheck/
   killfeed-filtered; item → industry calc/market/fitting). Cheapest compounding IA win; this
   is how Dotlan/zKB/EVERef win — deep-linking each other. Vigilant can do it internally.
5. **Breadcrumbs only where hierarchy exists** (Characters→Name→sub, Corp→Name→sub). Not on
   flat tool pages.
6. Skip mega-menus and auto-resume-last-page.

---

## Part 2 — Execution plan (tiered)

**Model tiering rule of thumb**
- **Opus 4.8**: async/session correctness, anything querying the 192GB killmails table, new
  architecture (nav registry, palette endpoint, snapshot jobs), features needing design
  judgment or EXPLAIN verification on the VPS.
- **Sonnet**: mechanical sweeps following an established in-repo pattern, template/consistency
  work, logging additions, config/cleanup, rollouts of a macro/pattern Opus designed.
- Each phase = one plan doc via superpowers writing-plans, executed with the usual
  commit→push→deploy flow and pre-deploy checklist.

### Phase 0 — Bug fixes & hygiene (do first; ~1–2 sessions)

| Task | What | Model | Size |
|------|------|-------|------|
| 0.1 | Fix BUG-1 + BUG-2: per-coroutine `AsyncSessionLocal()` in dashboard.py gathers (pattern from mining.py:284 / esi/market.py:63); verify dashboard sync + asset structure names post-deploy | **Opus 4.8** | M |
| 0.2 | BUG-4: EXPLAIN killfeed-search count path on VPS; enforce default time bound / reject unbounded searches (fold into T-037) | **Opus 4.8** | S |
| 0.3 | BUG-3: bound or SQL-window `streaks()`; keep result parity for a test character | **Opus 4.8** | S-M |
| 0.4 | BUG-5 + auth enrichment: add logging to swallowed exceptions | Sonnet | XS |
| 0.5 | BUG-6: semaphore on fittings gather (after cache-hit check) | Sonnet | XS |
| 0.6 | Migrate status.html + status_data.html to design system (fold into T-039) | Sonnet | S |
| 0.7 | Security: delete stale nginx/vigilant.conf (VVP-2026-007), escape notifications.js innerHTML (VVP-2026-021), harden docs/nginx-sample.conf (VVP-2026-020); mark-attempted via sec_findings.py | Sonnet | S |

### Phase 1 — Navigation foundation (the architectural fix)

| Task | What | Model | Size |
|------|------|-------|------|
| 1.1 | **Nav registry**: single Python source of truth (groups, labels, URLs, icons, active-path rules, admin flag) feeding desktop nav, mobile menu, footer, AND landings.py card grids. Kills the 4-way duplication permanently | **Opus 4.8** | M-L |
| 1.2 | Regroup + rename using the registry: Structure Age → Tools everywhere; wormhole reference pages into a Map/Wormholes group with Star Map + `/map/wormholes` + Wanderer link; "Watchlist" naming unified; dropdowns ↔ landing grids reconciled | Sonnet (after 1.1) | S |
| 1.3 | De-orphan: nav/landing entries for `/characters`, `/trending`, `/intel/kills/search`, `/map/wormholes`; `/status` linked from Admin; 301 redirects `/dscan*` → `/intel/dscan*` | Sonnet (after 1.1) | S |
| 1.4 | Page-header + breadcrumb consistency sweep: `.b-page-header` on the 11 missing pages; breadcrumbs standardized on char/corp subpages only | Sonnet | M (mechanical) |

### Phase 2 — Discovery & cross-linking

| Task | What | Model | Size |
|------|------|-------|------|
| 2.1 | **Ctrl+K command palette** + `/nav/palette` universal-search endpoint (pages from registry + characters + systems + items; recents-first) | **Opus 4.8** | M |
| 2.2 | Pinned + Recent in nav (localStorage, matches existing UI-pref pattern) | Sonnet | S |
| 2.3 | `entity_links` macro design + first integration (char detail, killfeed, map) | **Opus 4.8** design, Sonnet rollout | M |
| 2.4 | Rollout entity_links across remaining templates | Sonnet | M (mechanical) |

### Phase 3 — Quick-win features

| Task | What | Model | Size |
|------|------|-------|------|
| 3.1 | Discord alert relay for existing alert types (webhook; per-alert-type toggle) | Sonnet (pattern proven in thunderborn-ops) | S |
| 3.2 | EVE-Scout Thera/Turnur/Zarzakh: ingest public API, dashed edges on star map, "via Thera" option in router | **Opus 4.8** (router/graph math) | S-M |
| 3.3 | PWA manifest + icons | Sonnet | XS |

### Phase 4 — Market data foundation + dependents

| Task | What | Model | Size |
|------|------|-------|------|
| 4.1 | **Market price-history**: ingest strategy (ESI history endpoint vs EVERef bulk), storage schema with growth budget (learn from 192GB killmail surprise — estimate first!), per-type chart page + hub order-book view | **Opus 4.8** | L |
| 4.2 | LP store ROI calculator (SDE offers + market prices) | Sonnet (after 4.1) | S-M |
| 4.3 | "What should I build?" profitability ranking + invention/decryptor math | **Opus 4.8** (after 4.1) | M-L |

### Phase 5 — Big bets (pick by appetite)

| Task | What | Model | Size |
|------|------|-------|------|
| 5.1 | Net-worth tracker: daily valuation snapshot job (T-040 aggregate-table pattern) + chart | **Opus 4.8** design, Sonnet UI | M |
| 5.2 | Entity stats from local killmail archive (heatmaps, solo/gang, danger) — pre-aggregated tables only, never raw scans | **Opus 4.8** | M |
| 5.3 | Trade & industry P&L (FIFO matching engine) | **Opus 4.8** | L |
| 5.4 | Stockpile watchlists (character-level) | Sonnet (corp-threshold pattern) | M |
| 5.5 | Fit comparison + damage profiles + implants (existing tracked gap) | **Opus 4.8** | M |

### Relationship to existing tickets
- T-037 (killfeed search hardening) absorbs task 0.2.
- T-039 (restyle deferred fixes) absorbs task 0.6.
- T-034/T-035/T-036/T-038 unaffected; T-038 (ANALYZE strategy) becomes more valuable once
  Phase 4/5 add query load.
- Deferred "killfeed visible row cap" (memory) can ride along with Phase 0 or 2 as a Sonnet task.

### Suggested cadence
Phase 0 first (correctness before features). Phase 1 before Phase 2 (palette wants the
registry). Phases 3 can interleave anywhere. Phase 4 before 4.2/4.3 and ideally before 5.3.
Each phase gets its own brainstorm-lite → plan doc → execute cycle; Opus 4.8 tasks get full
plan docs, Sonnet tasks can run from checklist-style plans with explicit file lists.
