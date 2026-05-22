# Kill Feed — Most Valuable, Last 7 Days

**Status:** Drafted 2026-05-22.
**URL:** `/intel/kills` (additive — strip rendered above the existing live feed)
**Owner:** vigilant-vps repo
**Predecessor:** `2026-05-21-kill-activity-feed-design.md` (the live feed itself, already shipped)
**Successor:** Advanced Search at `/intel/kills/search` — separate spec, planned next

## Motivation

The `/intel/kills` page is a real-time tail of universe-wide kills. zKillboard's
landing page complements that with a "Most Valuable Structures / Ships / Sponsored
Killmails — Last 7 Days" strip that summarises what's been happening at the very
top end of ISK destroyed (see user-supplied screenshots).

Vigilant already retains every killmail forever (`KILLMAIL_GC_ENABLED` defaults
off — `app/intel/killmail_store.py:233`), so the 7-day top-N query is a cheap
read against existing data. The SDE `category_id` is backfilled on the
`sde_types` table during SDE load (`app/sde/loader.py:524`), giving us a one-join
filter for "structure vs ship" without any new ingestion work.

This delivery is **Feature A** of a two-feature kill-feed expansion. Feature B
(zKill-style advanced search with paginated historical results) gets its own
spec once Feature A is shipped — splitting the work keeps the live-feed JS
(five recent bug-fix commits) stable.

## User-facing design

### Placement

A collapsible strip at the top of `/intel/kills`, above the filter bar.
Default open; collapse state persists in `localStorage['vigilant:kf:topstrip']`.

```
┌─ /intel/kills ─────────────────────────────────────────────────────────────┐
│ KILL FEED                                                  LIVE · 1h · ... │
│                                                                            │
│ ▾ MOST VALUABLE · LAST 7 DAYS                                              │
│                                                                            │
│   STRUCTURES                                                               │
│   ┌──────┬──────┬──────┬──────┬──────┬──────┐                              │
│   │ Fort │ Soti │ Fort │ Tata │ Fort │ Fort │   (6 cards)                  │
│   │  •   │  •   │  •   │  •   │  •   │  •   │                              │
│   │ 33B  │ 30B  │ 20B  │ 19B  │ 18B  │ 16B  │                              │
│   │ corp │ corp │ corp │ corp │ corp │ corp │                              │
│   └──────┴──────┴──────┴──────┴──────┴──────┘                              │
│                                                                            │
│   SHIPS                                                                    │
│   ┌──────┬──────┬──────┬──────┬──────┬──────┐                              │
│   │Nomad │ Hel  │ Hel  │ Nyx  │Viator│ Nyx  │                              │
│   │ ...  │ ...  │ ...  │ ...  │ ...  │ ...  │                              │
│   └──────┴──────┴──────┴──────┴──────┴──────┘                              │
│                                                                            │
│  [open kill-detail panel renders here on card click]                       │
│                                                                            │
│ ─ filters ─────────────────────────────────────────────────────────────    │
│ [existing chip filter rows]                                                │
│                                                                            │
│ [existing live feed]                                                       │
└────────────────────────────────────────────────────────────────────────────┘
```

### Card content (one per kill)

- **Top:** 96×96 render from `https://images.evetech.net/types/{ship_type_id}/render?size=128`.
  Render (not icon) matches zKill's card art and the existing detail-panel
  victim card pattern (`partials/intel_kills_detail.html:7`).
- **Type name:** ship/structure type (`Fortizar`, `Hel`, …) — small, accent
  colour, single line, truncate with ellipsis if needed.
- **ISK total:** formatted to two decimals — `"33.44B"` for ≥1B, `"950.00M"`
  for <1B (we don't expect <1B kills to make the top 6, but cover the edge).
  Yellow, bold (`#facc15`, matches existing `.kf-isk`).
- **Victim corp:** corporation name (alliance as fallback if victim has no
  corp), muted (`var(--muted)`), single line, ellipsis.
- **System band tint:** thin left-edge or border-bottom in the system's band
  colour (HS green / LS yellow / NS red / WH purple) — reuses the existing
  `kf-sys-*` colour vars.

### Card click behaviour

Click → open the existing killmail detail partial inline in a slot directly
below the strip (one detail open at a time, scoped to the strip — independent
of any details open inside the live feed).

- Endpoint reused: `GET /intel/kills/{killmail_id}/detail` — already returns a
  self-contained `<div class="kf-detail">` block (`intel_kills.py:557`).
- Slot: `<div id="kf-top-detail-slot"></div>` between the Ships row and the
  filter bar. Render the partial there; clicking the same card again
  collapses (matches the live-feed accordion pattern).
- The strip's open card gets the same `.open` highlight class the feed rows use.

### Collapse / expand

Chevron toggle on the header (`▾ / ▸`). Collapsed: hide both rows, keep the
header visible so it's discoverable. State key: `vigilant:kf:topstrip`
(`true` / `false`).

### Empty state

Per row: if a category has no kills in the last 7 days (vanishingly unlikely
in practice — universe-wide 7d Ship kills are guaranteed), show "No kills in
the last 7 days." centred in muted small text. Per-category, so an empty
Structures row doesn't hide a populated Ships row.

### What we are NOT showing

- **No "Sponsored Killmails" row.** zKill monetisation only — not applicable.
- **No alliance / pilot summary rows.** Top-end ISK destroyed only, by ship
  and by structure.
- **No region / system / corp scope filter on this strip.** Universe-wide
  always. The live feed and the future advanced-search page handle scoping.
- **No "Hour-of-day" or "Most Active Pilot" sub-aggregates.** Out of scope.

## Backend design

### Route

New endpoint in `app/routes/intel_kills.py`:

```
GET /intel/kills/top
```

- Auth: same as existing `/intel/kills` routes (`request.session["user_id"]`
  required, 401 otherwise — matches existing handlers).
- Loaded by htmx on page load (`hx-get="/intel/kills/top" hx-trigger="load"`)
  so the strip doesn't block the main page render.
- Returns `partials/intel_kills_top.html` with a context dict (see SWR
  caching below).

### Data source

Single Killmail table read, joined to `sde_types` for the category filter:

```sql
-- Structures (category_id = 65)
SELECT k.killmail_id, k.killmail_time, k.solar_system_id,
       k.victim_ship_type_id, k.victim_character_id,
       k.victim_corporation_id, k.victim_alliance_id,
       k.total_value
FROM killmails k
JOIN sde_types t ON t.type_id = k.victim_ship_type_id
WHERE t.category_id = 65
  AND k.killmail_time >= :cutoff      -- now - 7 days, naive UTC
  AND k.total_value IS NOT NULL
ORDER BY k.total_value DESC
LIMIT 6;
```

Repeat with `t.category_id = 6` for ships. Run the two queries
**sequentially** inside `_compute_top_context` using a single
`AsyncSessionLocal()` — per CLAUDE.md's "Async session safety" gotcha, an
`AsyncSession` is not safe for concurrent statements even on read-only
queries. Each query targets a ~100K-row filtered slice and is expected to
return in well under 100ms, so sequential is fine. (If we ever fan out
beyond two queries here, switch each into its own helper that opens its
own session, then `asyncio.gather` them — same pattern as
`app/intel/kill_queries.py`.)

**Capitals**: zKill has a distinct "Capitals" filter in the menu screenshot;
we deliberately roll capitals into "Ships" here (categoryID=6 covers ship
groups including Carrier/Dread/Super/Titan/Freighter). Splitting Capitals
out is a Feature B concern — it's a filter dimension there. On the strip,
the "Ships" row will naturally surface big capital kills since they
dominate top-end ISK.

**Pods**: category_id = 6 includes Capsule. Pods are tiny ISK; they will
not realistically make the top-6, so no exclusion is needed.

### Index check (verify, deploy if needed)

The existing `ix_killmail_system_time(solar_system_id, killmail_time)` does
not help this query (no system predicate). The single-column
`Killmail.killmail_time` index gates the 7-day window down to ~100K rows
(at the ~15K/day ingest rate), after which the ORDER BY does a small sort.

Plan: deploy the route as-is and `EXPLAIN QUERY PLAN` on the VPS before
declaring the feature done. Add an index only if EXPLAIN shows a full table
scan:

```sql
CREATE INDEX IF NOT EXISTS ix_killmails_time_value
    ON killmails(killmail_time, total_value);
```

Per `feedback_create_all_skips_indexes`, `Base.metadata.create_all` ignores
indexes added to existing tables — ship any index via the post-deploy
`CREATE INDEX IF NOT EXISTS` step (already wired in `_create_missing_indexes`,
or run manually via `docker exec`).

### Name resolution

Each row needs:

- Victim ship type name (SDE local — already cached in
  `type_ids_to_names`).
- Victim corp name (ESI bulk `/universe/names` via the existing
  `resolve_entity_names` cache).
- Victim alliance name fallback if corp is null.
- System name (SDE local — `SDESystem.system_name`).
- System band for tint colour (`_sys_meta_cache` or
  `_resolve_sys_meta`).

All resolvers exist; reuse `_enrich_kills` style aggregation. Names are
cached in the shared name-resolver tables, so beyond the first hit per ID
per cache window this is free.

### SWR cache

Apply the proven dashboard pattern (per `feedback_swr_panel_caching`):

```python
_top_cache: dict[str, dict] = {}
_top_revalidating: set[str] = set()
_TOP_TTL = 300  # 5 minutes — top-6 rankings change at most a few times an hour

async def _compute_top_context(db) -> dict:
    """Run the two queries, resolve names, build the context dict."""
    ...

async def _refresh_top_background() -> None:
    key = "v1"
    try:
        async with AsyncSessionLocal() as bg_db:
            ctx = await _compute_top_context(bg_db)
        _top_cache[key] = {
            "context": ctx,
            "expires_at": datetime.now(timezone.utc) + timedelta(seconds=_TOP_TTL),
        }
    except Exception as e:
        log.info("kill-top SWR refresh failed: %s", e)
    finally:
        _top_revalidating.discard(key)
```

The cache key is a fixed string `"v1"` since the data is **per-universe, not
per-user** — same answer for every authenticated viewer. (If we add a corp
or region scope later, the key gets `(corp_id, region_id, …)` shape.)

**Cache the dict, not the rendered HTML** — the partial includes the same
CSP nonce concerns as other Vigilant partials. Re-render every request from
the cached context (~5-10ms).

### Storage / load expectations

- Query window: 7 days × 15K kills/day ≈ **105K rows scanned** per query,
  with the time-index narrowing the scan. Two queries (ships, structures)
  per cold cache fill.
- Cache fill cost on cold path: target <300ms wall time (verify with
  `VIGILANT_PERF_LOG=1`).
- Cache TTL 5 min → at peak with one user clicking around the page every
  minute, cold cost is paid once per 5 min. With multiple concurrent users
  the first concurrent request pays the cost, the rest serve cached.

## Implementation outline

(Concrete tasks are produced by the writing-plans skill — this is the shape.)

1. **Add the `/intel/kills/top` route + SWR cache + query.**
   `app/routes/intel_kills.py`. Module-globals for `_top_cache` /
   `_top_revalidating`. New `_compute_top_context` helper.
2. **Build the partial template.** New
   `app/templates/partials/intel_kills_top.html`. Two `<section>`s, six
   `.kf-top-card` divs each. CSS lives in `intel_kills.html` next to the
   existing kill-feed styles.
3. **Wire the strip into `intel_kills.html`.** Add the wrapper div above the
   filter bar with `hx-get` / `hx-trigger="load"`. Add the
   `#kf-top-detail-slot` underneath. Add the chevron + collapse-state JS.
4. **Card click handler.** Same fetch pattern as the existing
   `bindRowClicks` — fetch `/intel/kills/{kid}/detail`, inject into
   `#kf-top-detail-slot`, animate-expand. Re-clicking the same card
   collapses. Clicking a different card swaps the panel.
5. **EXPLAIN-check the query post-deploy.** Add `CREATE INDEX IF NOT
   EXISTS` only if needed.

## Non-goals (deliberate)

- **Live updates to the strip.** 5-minute staleness is fine; the data is
  inherently coarse ("top kills of the last 7 days").
- **Per-region or per-corp variants.** Universe-wide only.
- **CSV / data export.** Easy to add later.
- **"Most Valuable Alliances" / "Top Pilots" / "Top Damage Dealers".**
  Different aggregations; out of scope.
- **Sponsored Killmails row.** zKill monetisation, not relevant here.
- **Capital sub-row separate from Ships.** Rolled into Ships row; Capitals
  becomes a filter dimension in Feature B's advanced search.

## Open verification (during implementation)

- Confirm `sde_types.category_id` is populated on the VPS DB
  (`SELECT COUNT(*) FROM sde_types WHERE category_id IS NULL` — expect 0).
  If non-zero, run the loader's backfill (`app/sde/loader.py:524`).
- EXPLAIN the top-N query on the live DB and decide on the
  `(killmail_time, total_value)` index.
- Verify SWR cold-path latency under `VIGILANT_PERF_LOG=1` after the first
  deploy — should be well under 500ms.

## Future work — Feature B (separate spec)

`/intel/kills/search` — zKill-style advanced search:

- Filter dimensions: time range (24h / 7d / 30d / 90d + date-range picker),
  location (HS / LS / NS / WH + WH sub-class + Shattered), gang size chips,
  ISK chips, primetime hour-of-day chips, victim category (Ship / Structure
  / Capital), victim-ship autocomplete (existing), attacker entity
  autocomplete (existing), victim entity autocomplete (existing), NPC
  include/exclude/only toggle.
- Sort: Date / ISK / Involved — Asc/Desc.
- Pagination: cursor-style (`WHERE killmail_id < last_seen ORDER BY
  killmail_id DESC LIMIT 100`).
- Querystring + localStorage URL state, bookmarkable.
- **Non-goals carried into Feature B:** Awox / Padding / HighSec Gank
  heuristics; Damage sort; Points sort; AT Ships / POS / Anchored / PI /
  Sov category filters; Abyssal filter; CSV export; And/In/Or boolean
  picker between attacker/either/victim categories.

To be drafted as its own spec after Feature A is in production.
