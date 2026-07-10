# EVE-Style Tree Pickers (Build Finder + LP Store) — Design Spec

**Date:** 2026-07-10 · **Status:** approved

Replace two huge flat `<select>`s with in-game-market-style browsers:
Build Finder's 484-option inv-group select becomes an expandable
**market-group tree** (Ships → Frigates → Assault Frigates …) with search;
LP Store's full-roster corp select becomes a **faction → corp tree** with
search. Ranking math, invention controls, caps, offers tables: untouched.

## Build Finder

- **Tree**: lazy per-level expansion off `SDEMarketGroup`
  (parent_group_id tree, already imported; fitting's
  `/tools/fitting/browse/groups` is the precedent idiom).
  `GET /industry/build-finder/tree?parent=<id|0>` returns an htmx `<ul>`
  fragment: expand arrow when the node has children, every node clickable
  to select it.
- **Selecting any node (branch or leaf)** ranks all buildable products in
  its SUBTREE: new `sde.get_market_group_subtree_products(db,
  market_group_id, cap)` — load the (id → parent) map once (~2.5k rows),
  compute the descendant set in Python (BFS), then one products query
  (published + has blueprint, materials attached) shaped EXACTLY like
  `get_group_buildables`' output so the route/rank composition is
  unchanged. Cap 200 + "showing N of M" as today.
- **Search**: `GET /industry/build-finder/tree/search?q=` (min 2 chars) →
  fragment of up to 30 matching groups, each labeled with its full path
  via existing `get_market_group_path` ("Ships > Frigates > Assault
  Frigates"), clickable like tree nodes.
- **Selection state**: hidden `<input name="market_group_id">` inside the
  existing htmx form + a visible selected-path label; node click sets the
  input + label and triggers the existing form submit (no hx-get
  mutation — the known htmx gotcha).
- **Results endpoint**: `build_finder_results` takes `market_group_id`
  (replaces `group_id`; page is the only caller). The flat-select
  `get_buildable_groups` usage is removed (delete the lookup fn if no
  other callers remain).

## LP Store

- **Faction mapping**: `app/market/lp.py` gains a module-scope cached
  corp→faction map: ESI `GET /corporations/{id}/` per roster corp
  (public, `faction_id` optional) using the site's ESI bulk pattern
  (semaphore 3, batches of 10, 1s sleep), + `GET /universe/factions/`
  for names. Single-flight lock, failure not cached — same discipline as
  the existing roster cache. Corps without faction_id → "Other".
- **Tree**: `GET /market/lp/corps-tree` renders the whole two-level tree
  in one fragment (~270 corps — no lazy loading): faction headers
  (collapsible, majors first alphabetically, then others, "Other" last)
  with corps beneath. Corp click sets the existing `corporation_id`
  mechanism and loads offers exactly as today.
- **Search**: client-side filter input (all nodes already in DOM): hides
  non-matching corps, hides emptied factions, expands factions with hits.

## Error handling

- Tree endpoints auth-gated like their pages (empty/401 fragment).
- ESI faction fetch failure → tree renders with all corps under "Other"
  + a muted retry note; next request retries (failure not cached).
- Unknown/absent market_group_id → results endpoint returns the existing
  "pick a group" empty state.

## Testing

- Subtree resolver: 3-level fixture tree, products at multiple depths,
  descendant-set correctness, cap behavior, empty subtree.
- Tree + search endpoints: fragment shape, auth, path labels.
- LP: faction grouping with mocked ESI (corp with faction, without,
  fetch-failure path), tree fragment shape.
- Existing build-finder route tests updated from group_id fixtures to
  market_group_id + seeded SDEMarketGroup rows.

## Execution

Opus: subtree resolver + search lookup. Sonnet: Build Finder endpoints/
template swap; LP faction mapping. Haiku: LP tree template + client-side
filter JS (mechanical, precisely specified). Fable reviews each task.
