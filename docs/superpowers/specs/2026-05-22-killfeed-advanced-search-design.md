# Kill Feed — Advanced Search

**Status:** Drafted 2026-05-22.
**URL:** `/intel/kills/search`
**Owner:** vigilant-vps repo
**Predecessor:** `2026-05-22-killfeed-most-valuable-7d-design.md` (Feature A — Most Valuable strip; shipped).
**Phasing:** Two implementation plans (see [Phasing](#phasing) at the end).

## Motivation

Vigilant has a live tail at `/intel/kills` (the Kill Feed) and a recently-added
"Most Valuable last 7 days" strip at the top of that page (Feature A). What the
live tail can't do is **historical research** — "show me every Catalyst kill in
Jita over the last 30 days" or "show me all kills where Thor was in a
Basilisk." zKillboard's Advanced Search (screenshot from user, last-7-days
view) is the inspiration here.

This delivers an `/intel/kills/search` page with full filter UI, cursor
pagination, optional live polling on the result list, and the same row layout
+ detail-panel UX as the live feed. We re-use Feature A's autocomplete
endpoints (`/intel/kills/resolve?kind=ship|entity`) and the live feed's row
partial — the page is mostly composing existing parts behind a new filter
compiler.

## User-facing design

### Page placement

- URL: `/intel/kills/search`
- Sibling of `/intel/kills`. Reached via a link at the top of the Kill Feed
  page (added in the implementation plan), and by direct URL/bookmark.

### URL state

Querystring-driven and bookmarkable. localStorage shadow restores last filters
on an empty-querystring reload, matching Feature A's `/intel/kills` pattern.

```
/intel/kills/search?time=7d&space=hs,ls&isk=10b&count=51-100&primetime=eu
                  &sort=isk&dir=desc&attacker_mode=in
                  &attacker=ship:21628,char:90000001
                  &live=1
```

### Default state (no querystring)

Last 100 kills universe-wide, sorted by `killmail_time DESC`. **No time
filter** unless the user adds one. This is intentionally wide-open — the
page's purpose is browsing, not pre-narrowing.

### Layout

```
┌─ /intel/kills/search ──────────────────────────────────────────────────────┐
│ KILL FEED · ADVANCED SEARCH                              [Reset] [Save URL]│
│                                                                            │
│ ┌─── Filters ──────────────────────────────────────────────────────────┐   │
│ │ Time      [24h] [7d] [30d] [90d]  ·  from [____] to [____]           │   │
│ │ Space     [HS] [LS] [NS] [WH] [Abyssal]                              │   │
│ │ WH class  [C1] [C2]…[C6] [Thera] [Drifter] [Pochven] [Shattered]     │   │
│ │ Category  [Ship] [Structure] [Capital]                               │   │
│ │ Count     [Solo] [2-5] [6-10] [11-25] [26-50] [51-100] [100+]        │   │
│ │ ISK       [100m+] [1b+] [5b+] [10b+] [100b+] [1t+]                   │   │
│ │ Primetime [Aus/CN] [EU] [RU] [USE] [USW]                             │   │
│ │ Ship      <autocomplete>  [chips]                                    │   │
│ │                                                                      │   │
│ │ Attackers  [And] [In] [Or]   <autocomplete (entity + ship)> [chips]  │   │
│ │ Either     [And] [In] [Or]   <autocomplete>                  [chips] │   │
│ │ Victim     [And] [In] [Or]   <autocomplete>                  [chips] │   │
│ │                                                                      │   │
│ │ Sort: [Date▾] [ISK] [Involved]   Direction: [Desc] [Asc]   [☐ Live]  │   │
│ └──────────────────────────────────────────────────────────────────────┘   │
│                                                                            │
│ Showing 1-100 of 1,247  ·  total 8.4t ISK destroyed                        │
│ ┌──────────────────────────────────────────────────────────────────────┐   │
│ │ [row] Pilot [Corp] · Hel · system · killed by … · 33B    1m ago [NPC]│   │
│ │ [row] …                                                              │   │
│ │ (100 rows; click any to expand detail panel)                         │   │
│ └──────────────────────────────────────────────────────────────────────┘   │
│ [↓ Show more]                                                              │
└────────────────────────────────────────────────────────────────────────────┘
```

### Filter dimensions (Plan 1 — MVP)

| Dimension | Source | Notes |
|---|---|---|
| Time | `Killmail.killmail_time` | Chips 24h / 7d / 30d / 90d AND date-range text inputs (`YYYY-MM-DD HH:MM`, UTC, plain `<input type="text">` per `feedback_ui_preferences`). No default — empty time filter shows the most recent N kills universe-wide. |
| Space | SDE `security` + `system_id` band rules | HS / LS / NS / WH / **Abyssal** (`system_id BETWEEN 32000001 AND 32000200`, verified via SDE on 2026-05-22). Multi-select OR. |
| WH sub-class | `_sys_meta_cache` group_label | C1-C6 / Thera / Drifter / Pochven / Shattered modifier. Shown only when WH selected. AND with Space. For historical kills (not in `_sys_meta_cache`), sub-class filter requires a per-row SDE join — accept the cost for the search page since this is research, not live tail. |
| Category | SDE `category_id` + `group_id` on victim ship | **Ship** = category 6. **Structure** = category 65. **Capital** = `group_id IN (547, 485, 30, 659, 513, 902, 1538)` (Carrier / Dread / Titan / Super / Freighter / JF / FAX) **OR** `type_id = 28352` (Rorqual specifically — its group 941 also contains Porpoise and Orca which are NOT capitals). Multi-select OR. |
| Count (gang size) | `Killmail.attacker_count` | Chips Solo / 2-5 / 6-10 / 11-25 / 26-50 / 51-100 / 100+ |
| ISK | `Killmail.total_value` | Chips 100m+ / 1b+ / 5b+ / 10b+ / 100b+ / 1t+ |
| Primetime (UTC hour-of-day) | `strftime('%H', killmail_time)` | Chips Aus/CN (10-18) / EU (18-02) / RU (14-22) / USE (23-07) / USW (02-10). Wraparound bands implemented as `OR` (e.g. EU = `hour >= 18 OR hour < 2`). |
| Ship | SDE autocomplete | Multi-select OR over `victim_ship_type_id`. Reuses `/intel/kills/resolve?kind=ship`. |
| Attackers / Either / Victim entities | Combined autocomplete | Each side accepts char, corp, alli, AND ship type entries. Multi-select with mode toggle [And] [In] [Or] — see semantics below. Reuses `/intel/kills/resolve?kind=entity` plus the ship endpoint, surfaced behind one combined autocomplete UX. |

### And / In / Or semantics

The three modes compose listed predicates differently within one side.
The Victim and Attacker sides behave differently because a kill has many
attackers but exactly one victim — so Victim "And" is semantically the
same as Victim "In" (multiple predicates on the single victim row).

**Attackers side** (`killmail_attackers` rows joined via EXISTS):

| Mode | Shape |
|---|---|
| **Or** | One `EXISTS` with disjunctive predicates: `EXISTS (SELECT 1 FROM killmail_attackers a WHERE a.killmail_id = k.killmail_id AND (a.character_id IN list OR a.corporation_id IN list OR a.alliance_id IN list OR a.ship_type_id IN list))`. Any attacker matching any listed entity/ship. |
| **And** | N `EXISTS` clauses ANDed together — one per listed entity. Multiple separate attackers, each matching a different listed entity, all on the same kill. |
| **In** | One `EXISTS` with conjunctive predicates: `EXISTS (SELECT 1 FROM killmail_attackers a WHERE a.killmail_id = k.killmail_id AND a.character_id = X AND a.ship_type_id = Y)`. All listed predicates must hold on the SAME attacker row. Example: `Thor In Basilisk` → kills where Thor flew a Basilisk. |

**Victim side** (predicates against `killmails.victim_*_id` columns directly):

| Mode | Shape |
|---|---|
| **Or** | Disjunctive: `(k.victim_character_id IN list OR k.victim_corporation_id IN list OR k.victim_alliance_id IN list OR k.victim_ship_type_id IN list)`. Victim matches any listed entity/ship. |
| **In** | Conjunctive: `(k.victim_character_id = X AND k.victim_ship_type_id = Y)`. The single victim row matches all listed predicates. Example: `Thor In Basilisk` → kills where Thor died in a Basilisk. |
| **And** | Functionally identical to **In** — only one victim row exists per kill, so "all listed predicates on the same row" is the only meaningful interpretation. UI keeps the [And] button for parity with the Attackers row, but server-side it compiles to the In shape. |

**Either side** applies the same conjunction across both surfaces:

| Mode | Shape |
|---|---|
| **Or** | `(attacker Or-predicate satisfied) OR (victim Or-predicate satisfied)` — any listed entity appears anywhere on the kill. |
| **And** | Each listed entity must appear somewhere on the kill (attacker OR victim — different entities can land on different sides). N `(attacker_exists OR victim_match)` clauses ANDed. |
| **In** | `(attacker In-predicate satisfied) OR (victim In-predicate satisfied)` — all listed predicates resolve on the SAME attacker row, OR all on the victim row. Example: `Thor In Basilisk` on Either side → kills where Thor flew a Basilisk OR where Thor died in a Basilisk. |

**Cross-side composition:** Attackers AND Either AND Victim columns combine
with AND. Empty column = no constraint.

### Sort + cursor pagination

- Sort: **Date** (default) / **ISK** / **Involved (= `attacker_count`)**, with
  **Desc** (default) / **Asc**.
- Page size: **100 rows** per fetch.
- Pagination: **cursor-based**, no hard cap.
- Initial page: `LIMIT 100`, returns `(oldest_seen, newest_seen)` cursors.
- "Show more" button at the bottom: `WHERE <sort_cursor> ORDER BY <sort> LIMIT 100`.
- Sort cursors:
  - **Date Desc:** `WHERE killmail_id < <oldest_seen>` (killmail_id is
    monotonic and unique → simple cursor, no tuple needed).
  - **Date Asc:** `WHERE killmail_id > <oldest_seen>` (the "oldest" cursor
    in Asc mode is the youngest row currently shown).
  - **ISK / Involved (either direction):** `(sort_col, killmail_id)` tuple
    cursor — `WHERE (total_value, killmail_id) < (?, ?)` — to break ties on
    the sort column.

### Show more vs Live polling — coexistence

`Show more` walks BACK in time (extends the bottom of the list).
Live polling brings forward NEW kills (prepends to the top).

Two non-overlapping cursor windows:

- Live cursor: `killmail_id > <newest_seen>` — only new arrivals.
- Pagination cursor: `<sort_cursor>` against `<oldest_seen>` — only older.

A user can have a 500-row deep list with live polling still enabled — new
rows arrive at the top, old rows stay below. No reshuffling.

### Live toggle visibility

The Live toggle is **only enabled** when:

1. `sort = Date Desc`, AND
2. No end-date in the time filter (i.e. the time window extends to "now").

Otherwise it's greyed out with a tooltip ("Live updates require sort=Date Desc
and an open-ended time window"). When enabled, htmx polls
`/intel/kills/search/results?live=1&since=<newest_seen>&<filters>` every 15s
with the same dedupe + flash-animation pattern as the live feed.

### Row layout

**Reuse `app/templates/partials/intel_kills_feed.html`** — same row shape as
the live feed:

```
[ship icon 36×36]  Pilot [Corp] · Ship             ISK   timestamp
                   System · killed by ... · gang of N
```

Plus **NPC badge**: when `Killmail.is_npc = 1`, render a small `[NPC]` tag
inline with the gang-of-N line. This badge is added to the shared row partial,
so it also appears on the live feed page.

Clicking a row toggles the same detail panel partial used everywhere else
(`/intel/kills/{killmail_id}/detail`, cached 24h client-side).

### Stats header

Above the result list:

```
Showing 1-100 of 1,247 · total 8.4t ISK destroyed
```

`Showing 1-N` updates as the user clicks Show More.

`of 1,247` is the **filter result count** — a separate `COUNT(*)` query for
the filtered set. Computed once per filter combo (re-runs when filters change,
not on Show More). Acceptable because COUNT is cheaper than the SELECT (no
ORDER BY, no name resolution).

`total ISK destroyed` is `SUM(total_value)` over the filtered set, same
guidance.

## Backend design

### Routes

`GET /intel/kills/search`
- Returns the page shell (filter form, empty results container).

`GET /intel/kills/search/results`
- htmx-served. Accepts all filter params via querystring + a `cursor` and
  optional `live=1` flag.
- Returns: rendered row partial (multiple `kf-row` divs) AND a small marker
  div with `data-oldest`, `data-newest`, `data-total`, `data-total-isk`
  (parsed by client JS to update cursors + stats header).

`GET /intel/kills/search/stats`
- Optional separate endpoint for the COUNT + SUM, so the result rows render
  immediately and the count can stream in. (MVP-OK to bundle into
  `/results` and pay the cost up-front; split only if perf demands it.)

The existing detail endpoint (`GET /intel/kills/{killmail_id}/detail`) and
autocomplete endpoint (`GET /intel/kills/resolve`) are reused as-is.

### Filter compilation

A single `_compile_search_where(params)` helper builds the SQLAlchemy WHERE
clause. Each dimension adds its predicates conditionally; empty filters add
nothing. Output: `(where_clauses, join_tables_needed, sort_expression)`.

The compiler must:

1. Validate + normalize all params (`int()` coercion, allowed-list checks).
2. Refuse arbitrary SQL — only construct via SQLAlchemy column ops.
3. Apply the And/In/Or semantics correctly per side.
4. Honor the time-range constraints (chip-preset OR custom range, not both).

### Query shape

```sql
SELECT k.*
FROM killmails k
[optional: JOIN sde_types t ON t.type_id = k.victim_ship_type_id]
[optional: JOIN sde_systems s ON s.system_id = k.solar_system_id]
WHERE 1=1
  -- time
  [AND k.killmail_time >= ? AND k.killmail_time <= ?]
  -- ISK
  [AND k.total_value >= ?]
  -- gang size
  [AND k.attacker_count BETWEEN ? AND ?]
  -- primetime (one or more hour ranges OR'd together)
  [AND (strftime('%H', k.killmail_time) BETWEEN ? AND ? OR ...)]
  -- category
  [AND ((t.category_id IN (6,65)) OR (t.group_id IN (547,485,...)) OR (k.victim_ship_type_id = 28352))]
  -- space (HS/LS/NS/WH/Abyssal as a set of OR'd predicates)
  [AND (
        (s.security >= 0.5)
     OR (s.security > 0 AND s.security < 0.5)
     OR (s.security <= 0 AND s.system_id < 31000000)
     OR (s.system_id BETWEEN 31000000 AND 31999999)
     OR (s.system_id BETWEEN 32000001 AND 32000200)
  )]
  -- WH sub-class (only when WH selected; requires sde_systems.region/constellation join)
  -- victim ship
  [AND k.victim_ship_type_id IN (?,?,?)]
  -- attackers (And/In/Or — see semantic above)
  [AND <attackers predicate>]
  -- victim entity (And/In/Or — direct on victim_*_id columns)
  [AND <victim predicate>]
  -- either
  [AND <either predicate>]
  -- cursor (Show more)
  [AND <cursor predicate>]
ORDER BY <sort_col> <dir>, k.killmail_id <dir>
LIMIT 100
```

### Pagination cursor — token format

Pass cursor as a **single string** in the querystring:
- Date sort: `cursor=<killmail_id>` (one int).
- ISK / Involved sort: `cursor=<sort_val>:<killmail_id>` (compact, signed-safe
  for total_value floats).

Server-side validate + decode; refuse if malformed.

### Live polling — request shape

```
GET /intel/kills/search/results?live=1&since=<newest_killmail_id>&<filters>
```

Server returns only rows with `killmail_id > since` that match the filters,
LIMIT 100 (safety cap on a busy poll). Same dedupe + prepend logic as the
existing live feed (`htmx:beforeSwap` parses the response, drops rows already
in DOM by `data-kid`, prepends the rest with the `kf-new` flash animation).

### NPC badge — shared row partial change

Edit `app/templates/partials/intel_kills_feed.html` to render `[NPC]` when
the kill record has `is_npc` true. This requires `_enrich_kills` (live feed
side) AND the search-results enrichment to surface `is_npc` on each row dict.

The badge styling: `<span class="kf-npc-badge">NPC</span>` with subtle muted
colour (not a loud red — just an unobtrusive marker). CSS lives next to the
existing `.kf-row` rules in `app/templates/intel_kills.html` (live feed page)
plus the new search page.

### Name resolution

Same shape as Feature A's `_compute_top_context`: collect type + entity IDs
across the 100-row batch, resolve via `type_ids_to_names` (SDE local) and
`resolve_entity_names` (ESI bulk, cached). For cold filter combos with
unfamiliar entities the first page incurs an ESI hop (~500-2000ms total
depending on uncached fan-out). Subsequent pages with overlapping entities
hit the warm cache.

No SWR cache on `/intel/kills/search/results` — too many distinct filter
combos to amortize. The name-resolver cache provides the relevant warming.

### Indexes (verify-then-add)

After deploy, run EXPLAIN on the dominant query shapes. Likely candidates,
deployed via `CREATE INDEX IF NOT EXISTS` per `feedback_create_all_skips_indexes`:

- `(victim_ship_type_id, killmail_id DESC)` — category / ship filter
- `(total_value DESC, killmail_id)` — ISK sort + cursor
- `(attacker_count, killmail_id)` — Involved sort + cursor

Do not pre-add — verify with EXPLAIN against real traffic patterns first.

## Performance budget

- Default page (no filters, last 100, Date Desc): index-served on
  `ix_killmails_killmail_time`. Target cold path ~800ms (name resolution
  dominates), warm path ~100ms.
- Filtered page (e.g. system + 7-day window): same shape, slightly slower
  WHERE evaluation. Target <1.5s cold, <200ms warm.
- Live poll: every 15s while toggle is on. Each poll is a small `killmail_id >
  ?` query. Target <100ms per request.
- Show More: same query shape as initial page, different cursor. Same target.

## Phasing

### Plan 1 (MVP — ship first)

- Page skeleton + filter card UI
- All chip filter rows: Time / Space (incl. Abyssal) / WH sub-class / Category
  (Ship/Structure/Capital) / Count / ISK / Primetime
- Ship autocomplete
- Attackers / Either / Victim with And / In / Or mode + combined entity+ship
  autocomplete
- Sort (Date/ISK/Involved) + direction
- Cursor pagination + "Show more"
- Live toggle (conditional visibility + 15s poll + prepend dedupe)
- NPC badge added to shared row partial
- Querystring + localStorage URL state
- Post-deploy EXPLAIN sweep + index additions as needed

### Plan 2 (advanced flags + niche categories — ship second)

- **Awox** flag: `EXISTS (SELECT 1 FROM killmail_attackers a WHERE
  a.killmail_id = k.killmail_id AND a.corporation_id = k.victim_corporation_id)`
- **HighSec Gank** flag: HS location AND `EXISTS attacker with security_status < 0`
- **Padding** flag (v1 heuristic): kills with `attacker_count >= 5` AND
  `(count of attackers with damage_done < 1% of max_damage) / attacker_count >= 0.5`.
  Sparse — `damage_done` is only populated on ~4% of historical attacker
  rows (verified via `SELECT * FROM killmail_attackers WHERE damage_done > 0`
  count). Filter UI shows a "Padding" chip; selecting it returns only kills
  where the flag can be computed (rest are unknown). UI note: "Padding
  detection requires damage data, available on recently-ingested kills only."
- **AT Ships** category: hardcoded type_id list. Confirmed roster (17 ships)
  verified via SDE on 2026-05-22: `[2836, 29266, 32788, 33675, 33397, 85229,
  32790, 35781, 32207, 35779, 3516, 32209, 33395, 48635, 2834, 3518, 33673]`
  (Adrestia, Apotheosis, Cambion, Chameleon, Chremoas, Cobra, Etana, Fiend,
  Freki, Imp, Malice, Mimir, Moracha, Tiamat, Utu, Vangel, Whiptail). Compile
  full roster at plan time — adds Caedes / Echelon / Magnates / Reagent /
  Victor and any new releases. AT Ships matches kills where the victim
  OR any attacker is an AT ship (broader than the Capital category which is
  victim-only).
- Whatever UX polish the user surfaces from using Plan 1.

## Non-goals (deliberate)

- Sponsored Killmails (zKill monetization, not relevant)
- Damage sort (needs SUM over attackers per kill row + dependable damage_done
  data, neither cheap nor reliable yet)
- Points sort (zKill's scoring formula — not relevant)
- POS / Anchored / PI / Sov / Drone / Fighter categories (niche; defer until
  asked for specifically)
- CSV / XLSX export (easy to add later as a separate route)
- Saved filter presets (URL bookmarks cover this)
- Per-region filter scope (region chips would require region join + ID list;
  defer)
- Multi-tab "Top 7 days" panels in the search page (Feature A's strip stays
  on `/intel/kills`; the search page is for browsing the full table)
