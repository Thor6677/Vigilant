# Phase 5 — Big Bets Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers-extended-cc:subagent-driven-development.

**Goal:** The five remaining roadmap features, ordered smallest-risk-first with deploy gates between pairs: net-worth tracker, killboard-grade entity stats from the local archive, stockpile watchlists, fit comparison + damage profiles, and trade/industry P&L.

**Standing constraints (all tasks):** killmails table is ~60M rows/192GB — day+ windows NEVER scan raw killmails, use/extend pre-aggregated tables (T-040 pattern); container is 2.5GB/2CPU; new tables via `Base.metadata.create_all` (no existing-table column adds without a migration block in main.py); new pages register in `app/nav.py` (dead-link test enforces); route modules name their Jinja2Templates instance `templates`; per-coroutine `AsyncSessionLocal` for concurrent writes; charts follow the Chart.js idiom from market_type.html.

**Model tiering:** 5.1/5.4 design=Opus with Sonnet-ready specs; 5.2/5.3/5.5 Opus. Baseline suite: 133 passed.

---

### Task 1: Net-worth tracker [Opus]

Daily valuation snapshot per character + total: wallet + assets (priced via `/markets/prices/` map — `app/market/lp.get_price_map`) + sell-order escrow + industry-job value if cheap to include (investigate what's synced: char asset cache, orders, wallet in the dashboard sync caches). New table `net_worth_snapshots(user_id, character_id, date, wallet, assets_value, escrow, total)` (composite PK date+character). Snapshot job: piggyback the existing scheduler (find how daily jobs run — grep scheduler/cron in app/; e.g. killmail_daily_rollup pattern) — one snapshot/day at a fixed hour, idempotent upsert. Page `/tools/networth` (nav: Tools group): stacked chart (per character) + total line, 30d/90d/1y ranges; "snapshot now" button (htmx POST) so the user doesn't wait a day for the first data point. Valuation must NOT fetch per-item prices (one global price map). Assets from the existing synced asset cache JSON — parse, sum qty×price; unpriced items skipped with a count shown. Tests: valuation math with fixture assets/prices; upsert idempotency; route gating.

Commit: `feat(tools): net-worth tracker with daily snapshots`

### Task 2: Entity stats from the local killmail archive [Opus]

Extend the combat-profile surface with zKB-grade stats for ANY character/corp/alliance (not just own chars): activity heatmap (hour-of-day × day-of-week), solo vs gang ratio, top ships/systems, danger rating (simple: kills/(kills+losses))— computed from LOCAL killmails + killmail_attackers. CRITICAL: all-time queries banned; default window 90d (killmail_time-indexed), optional 1y via `killmail_daily_aggregates`-style pre-aggregation ONLY if an existing aggregate covers it — investigate `app/intel/kill_queries.py` + the T-040 aggregate tables first; if no per-entity aggregate exists, 90d/30d windows only (SQL GROUP BY on indexed time window; EXPLAIN-check on the VPS in the deploy gate). Page `/intel/entity/{kind}/{id}` + search-by-name entry on the killfeed/palette (palette characters bucket already links chars — add an "Entity stats" chip to entity_links for characters). Heatmap render: CSS grid of colored cells (no new chart lib). Tests: stat math on seeded fixtures; window enforcement (compiled SQL contains time bound).

Commit: `feat(intel): entity combat stats from local killmail archive`

### Task 3: Stockpile watchlists [Opus design, straightforward build]

Character-level stockpile targets: table `stockpile_targets(user_id, type_id, location_hint nullable, target_qty)`. Current holdings = synced asset cache (all chars) + open sell orders qty + in-progress manufacturing output qty (investigate what's in the synced caches; include what's cheap, document what's excluded). Page `/tools/stockpiles` (nav: Tools): CRUD rows (htmx), current vs target with deficit highlighted, type search reusing the market search partial idiom. Alert integration: when a stockpile drops below target during the dashboard sync, emit via the existing `_emit_notification` choke point (type `stockpile_low` — add to base.html notification settings panel + relay-eligible). Tests: netting math with fixtures; CRUD route gating.

Commit: `feat(tools): stockpile watchlists with sync-time alerts`

### Task 4: Fit comparison + damage profiles [Opus]

Extends the existing dogma fitting engine (app/fitting/ — read engine.py first; memory: spool formula, per-level detection gotchas). (a) Damage-profile selector on the fitting tool: uniform/EM/Therm/Kin/Exp presets + custom sliders, affecting effective HP/resists display (the engine computes resists; profile weighting is display-layer math). (b) Compare view: pick 2 saved fits → side-by-side stat table (EHP, DPS, speed, cap, etc.) with deltas highlighted. No implants in this task (defer, note). Frontend within the existing fitting_tool.html idiom (it's htmx/JS heavy — be careful with the ~22 fetch() calls noted in the audit; do not refactor them). Tests: profile-weighted EHP math unit test; compare endpoint returns both fits' stats.

Commit: `feat(fitting): damage profiles + fit comparison`

### Task 5: Trade & industry P&L (FIFO matching) [Opus — largest]

Realized-profit tracking: FIFO-match wallet transactions (buys→sells per type per character) using the synced wallet transaction data (investigate: is transaction history synced/stored, or only journal? If transactions aren't persisted, add a sync field following `_FIELD_FETCHERS` pattern + a `wallet_transactions` table — that's half the task). Matching engine (pure module `app/market/pnl.py`): per type_id FIFO queue, buys consumed by sells, realized profit = sell_net − matched_buy_cost (fees/taxes from journal if linkable, else flat-rate assumption documented); unmatched sells (pre-history) excluded with count. Industry P&L deferred to a follow-up ticket (manufacture-cost linkage is a separate beast — note it; this task ships TRADE P&L only, retitle page accordingly "Trading P&L"). Page `/market/pnl` (nav: Industry group): per-type realized profit table + monthly chart. Tests: FIFO engine exhaustively (partial fills, multiple lots, out-of-order timestamps, unmatched).

Commit: `feat(market): trading P&L with FIFO transaction matching`

### Task 6: Deploy + verify Phase 5 [coordinator]

Deploy after Tasks 1+2, again after 3+4+5 (or per-task if sessions stretch). Checklist per CLAUDE.md; new tables verified post-deploy; EXPLAIN the entity-stats queries ON THE VPS; watch memory; ticket a follow-up for implants + industry P&L + invention math. Final: refresh SECURITY_TODO snapshot, update roadmap spec statuses, close-out commit.
