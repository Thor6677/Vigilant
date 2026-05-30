# PCU Delta Display — Design Spec

**Date:** 2026-05-29  
**Status:** Approved

## Overview

Two related features:
1. **Dashboard server bar**: Show the 15-minute pilot count delta next to "X pilots online".
2. **Activity page live tile**: A new stat tile that polls every 60 seconds showing the current online count and the delta vs the previous ESI sample (~60s ago).

Both features draw from the existing `player_count_snapshots` table (source='esi', indexed on `recorded_at`). No new tables or background jobs are needed.

---

## Feature 1 — Dashboard 15-Minute Delta

### What changes

`GET /api/server-status` gains a new field `delta_15m: int | null`.

**Computation:**
- Fetch the latest `PlayerCountSnapshot` where `source='esi'` — this is the current count.
- Fetch the snapshot closest to `now - 15 minutes` (most recent row with `recorded_at <= now - 14 min`, i.e. within a 2-minute tolerance window around the 15-min mark).
- `delta_15m = latest.player_count - ref.player_count`, or `null` if no reference point exists.

Both reads are single-row queries on an already-indexed column; cost is negligible.

### Dashboard display

The existing `#server-players` span is updated to include the delta:

```
28,412 pilots online  +347
```

The delta badge is a styled `<span>` inline:
- `+N` → `color: var(--success)` (green)  
- `-N` → `color: var(--danger)` (red)  
- `null` / `0` → delta badge omitted

The dashboard poll cadence **stays at 15 minutes** — delta computation is bundled into the existing `/api/server-status` call, no new polling.

---

## Feature 2 — Activity Page Live Tile

### New endpoint

`GET /api/live-pcu` — returns HTML partial (not JSON), suitable for htmx outerHTML swap.

**Computation:**
- Latest `PlayerCountSnapshot` where `source='esi'` → current count and `recorded_at`.
- Previous snapshot: latest row where `source='esi'` and `recorded_at < current.recorded_at` (up to 5-minute lookback to avoid stale deltas).
- `delta = current.count - previous.count`, or `null` if no previous within 5 min.

**Response:** Renders `partials/live_pcu_tile.html` with `{count, delta, online, recorded_at}`.

### Template changes

In `tools_activity.html`, the existing 4-column stat grid gains a 5th tile rendered as a named partial:

```html
<div id="live-pcu-tile"
     hx-get="/api/live-pcu"
     hx-trigger="load, every 60s"
     hx-swap="outerHTML">
    <!-- placeholder shown before first load -->
    <div class="ta-stat">
        <div class="ta-stat-val" style="color:var(--muted)">—</div>
        <div class="ta-stat-label">Live pilots</div>
    </div>
</div>
```

The partial (`partials/live_pcu_tile.html`) wraps the tile in the same `id="live-pcu-tile"` div with the same htmx attributes, so every swap re-arms the 60s trigger. Content:

```
[count formatted as K]
LIVE PILOTS  [+N green | -N red | (blank if null)]
```

### Grid layout adjustment

The `.ta-stats` grid is currently `grid-template-columns: repeat(4, 1fr)`. The inline style on that element changes to `repeat(5, 1fr)` when the live tile is present (always shown on the activity page, so it's hardcoded).

---

## Non-Goals

- No SSE / WebSockets — polling every 60s matches the ESI sampler cadence and is sufficient.
- No change to `player_count.py` background job — it already runs every 60s.
- No change to dashboard refresh cadence — stays 15 minutes.
- No delta on the dashboard **activity overlay** (`/dashboard/activity` partial) — that panel already shows a chart; the server bar delta is sufficient there.

---

## Files Touched

| File | Change |
|------|--------|
| `app/routes/dashboard.py` | Extend `api_server_status` + add `api_live_pcu` endpoint |
| `app/templates/dashboard.html` | Update `updateServerStatus()` JS to render delta badge |
| `app/templates/tools_activity.html` | Add live tile placeholder + widen stat grid to 5-col |
| `app/templates/partials/live_pcu_tile.html` | New partial for htmx swap |
