# Phase 4 — Market Data Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers-extended-cc:subagent-driven-development.

**Goal:** Market price-history charts + hub order-book browser (the biggest feature gap vs Adam4EVE), then LP-store ROI and "what should I build?" profitability ranking on top.

**Storage design (locked — the 192GB lesson):** NO bulk ingest. History rows are fetched on demand per (region, type) — ESI `/markets/{region_id}/history/` returns ~400 days of daily rows in ONE call — and cached in a `market_history` table with a 24h freshness stamp. Rows accrue only for types the user actually looks at (or that ROI/profitability tools request). Worst case a few thousand types × 400 rows × ~60B ≈ tens of MB, not GB. Region default: The Forge (10000002). A `market_history_meta` row per (region,type) tracks `fetched_at` for TTL.

**Existing infra to reuse:** `app/esi/market.py` (get_market_prices global list, region orders fetch w/ pagination, per-coroutine sessions — the model citizen), `app/db/cache.py` TTL cache, nav registry (`app/nav.py` — new pages MUST be registered there; the dead-link test enforces), SWR panel pattern, chart stack already used elsewhere (check what the activity/wallet charts use — likely Chart.js or uPlot; match it).

**Model tiering:** Tasks 1, 4 Opus 4.8; Tasks 2, 3 Sonnet after the Task 1 foundation; deploy gates coordinator. Baseline suite: 96 passed.

---

### Task 1: Market history storage + service + chart page [Opus]

**Files:** `app/db/models.py` (MarketHistory + MarketHistoryMeta tables), `app/market/history.py` (new service: `get_history(region_id, type_id, db) -> list[rows]` — cache-first, ESI fetch on stale/missing via upsert, 24h TTL, semaphore(3) for any multi-type prefetch), `app/routes/market.py` (routes: `/market` browser page with type search reusing the palette's SDEType search idiom; `/market/type/{type_id}` chart page: price line (avg) + high/low band + volume bars, range toggles 30d/90d/1y/all), templates (`market.html`, `market_type.html` + partials), nav registry entry (Industry group: "Market" item — with desc/features for the landing card), tests.

**DB safety (CLAUDE.md):** new tables auto-create via `Base.metadata.create_all` — no migration block needed; NO new columns on existing tables. Chart data endpoint must aggregate/slice in SQL (indexed by (region_id, type_id, date) PK) — trivial here since per-type rows are ≤~450.

**Charting:** match the in-repo chart idiom (grep for Chart.js/uPlot/canvas usage in activity_history.html / journal.html). htmx-load the chart data as JSON endpoint + inline script INSIDE content block; CSP nonce.

**Tests:** service unit tests with monkeypatched ESI fetch (cache-miss fetches once; second call within TTL doesn't; stale refetches; upsert idempotent), route smoke via TestClient (auth-gated), nav dead-link test passes automatically.

Commit: `feat(market): price-history foundation — on-demand cached history + chart pages`

---

### Task 2: Hub order-book view [Sonnet, after Task 1]

On `/market/type/{type_id}`: buy/sell order tables for the hub region (top 15 each, price/volume/location), spread + margin percentage header. Reuse `app/esi/market.py`'s region-orders fetch filtered by type_id (check it supports type_id param — ESI does: `?type_id=`); short cache (5 min) via `app/db/cache.py` (add TTL match in `_ttl_for_path` if routing through the client cache — see gotcha). No new tables. Tests: monkeypatched fetch, spread math unit test.

Commit: `feat(market): hub order-book view with spread on type pages`

---

### Task 3: LP store ROI calculator [Sonnet, after Task 1]

**Investigate FIRST:** is the LP store offer data in the SDE tables already imported (grep sde models/loader for `lpOffer`/loyalty)? Likely NOT → use ESI `/loyalty/stores/{corporation_id}/offers/` (public, cacheable 24h via client cache — add TTL) + NPC corp list from SDE (`crpNPCCorporations` equivalent — check what's imported; if NPC corps aren't in SDE tables either, use ESI `/corporations/npccorps/` ids + names via existing name resolution). Page `/market/lp` (nav: Industry group): pick NPC corp → table of offers with required items, LP cost, ISK cost, market sell value (from Task 1 history latest or `/markets/prices/`), ISK/LP ratio, sorted desc. Per-offer material costs via required items' prices. Cap: compute on demand for one corp at a time (offers ≤ a few hundred). Tests: ISK/LP math unit test with fixture offers.

Commit: `feat(market): LP store ROI calculator`

---

### Task 4: "What should I build?" profitability ranking [Opus, after Task 1]

**Investigate FIRST:** how the manufacturing calculator computes cost (app/routes/industry.py `/industry/calculate` flow — reuse its cost engine as a function, do NOT duplicate math; if it's endpoint-tangled, extract the pure cost function first — behavior-identical refactor). Page `/industry/build-finder` (nav: Industry group): filter by market group / tech level, rank buildable blueprints by (sell value − build cost) margin and by margin %, using current prices (`/markets/prices/` or Task 1 latest-history) for outputs and inputs. SCOPE CAP: compute for a user-selected market-group subset (e.g. "Battleships"), NOT all blueprints at once; cap ~200 blueprints per request; SQL/vectorized where possible; show compute time. No invention math in this task (decryptor math = follow-up ticket; note it). Tests: ranking math with fixture costs/prices; cap enforcement.

Commit: `feat(industry): build-profitability finder (margin ranking per market group)`

---

### Task 5: Deploy + verify Phase 4 [coordinator]

Two-stage allowed (deploy after Task 1+2 if Task 3/4 lag). Checklist (new tables → create_all covers; confirm post-deploy that `market_history` exists via docker exec sqlite check) → push → deploy → prod checks: /market page renders, a type chart loads and populates `market_history` rows, order book renders, LP page renders for one corp, build-finder returns a ranked table. Watch container memory (2.5GB cap) during build-finder request. Sync tasks.json, close-out, ticket for invention-math follow-up.
