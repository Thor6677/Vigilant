# Recent Battles — Two-Column Rollup + Top-10

**Date:** 2026-05-26
**Widget:** Dashboard "Major Fleet Battles in New Eden — last 7 days"
**Trigger:** Card grid overflowed the 1360px container (`grid-template-columns: repeat(6, 1fr)` + `white-space:nowrap` content), and the previous design split K-space into an off-screen row list. User asked for "better ways of displaying this information" with `/effort max`.

## Goal

Replace the card grid with a two-column layout: a band-rollup table on the left (one row per band, sorted by activity) and a flat top-10-by-kills leaderboard on the right. Two columns side-by-side at desktop widths, stacks on narrow viewports.

## Why this shape (research notes)

- **DOTLAN stats** — stacks dense numbered top-10 lists, no cards. Mirror: the right column.
- **zKillboard home** — small card sections for "Most Valuable", then a macro stats block, then ranked tables. Mirror: the left column rollup as "macro stats per band".
- **Grafana stat-panel pattern** — big number + tiny detail, small-multiples. We use small-multiples for the per-band row.
- Common failure mode the old design hit: `1fr` grid tracks expand under `white-space:nowrap` content. We use `minmax(0, 1fr)` throughout so no track can blow past its share.

## Non-goals

- No drill-down expansion. A row click goes straight to zKillboard (matches existing behavior).
- No new server-side queries. We reuse `query_battles_window(days=7)` and aggregate in the route.
- No animated transitions, no live polling on this widget. It's a once-on-load partial via htmx `hx-trigger="load"` (unchanged).
- No backend caching for this widget specifically. The underlying query is already cheap (`detected_battles` is tiny).

## Layout

Two columns at viewport ≥ 900px. Stacks (left first, then right) below 900px.

```
┌────────────────────────────────────────────────────────────────────┐
│  MAJOR FLEET BATTLES IN NEW EDEN — LAST 7 DAYS                     │
├──────────────────────────────────────┬─────────────────────────────┤
│  BAND ACTIVITY                       │  TOP 10 BY KILLS            │
│  ─────────────                       │  ─────────────              │
│  [NS] 4-HWWF      3F · 600K · 132B   │   1 [NS] 4-HWWF   200K·363P │
│  [LS] Turnur      3F · 496K · 389B   │   2 [NS] 4-HWWF   200K·362P │
│  [HS] Jita        3F · 166K · 146B   │   3 [NS] CKX-RW   200K·303P │
│  [C2] J162430     2F · 262K          │   4 [C2] J162430  192K·81P  │
│  [PO] Skarkon     2F · 221K          │   5 [LS] Turnur   183K·266P │
│  [C3] J214744     2F · 165K          │   6 [LS] Turnur   173K·262P │
│  [C5] J170231     2F · 96K           │   7 [LS] Ahbazon  140K·102P │
│  [C4] J161029     2F · 66K           │   8 [PO] Skarkon  111K·170P │
│  [TH] Thera       2F · 54K           │   9 [PO] Skarkon  110K·169P │
│  [C1] J204640     2F · 34K           │  10 [C3] J214744   98K· 83P │
│  [C6] J105023     2F · 18K           │                             │
│  [DR] Sentinel    2F · 12K           │                             │
└──────────────────────────────────────┴─────────────────────────────┘
```

Square brackets `[XX]` represent colored band badges, reusing the existing palette:

- `NS` red (`#5a1a1a` bg / `#e07a7a` fg)
- `LS` amber (`#5a3a1a` / `#e0a85a`)
- `HS` green (`#1a4d2e` / `#7fd99a`)
- WH classes (C1–C6, Thera, C13, Drifter, Pochven) purple (`#2a1a4d` / `#a78bfa`)

## Components

### Backend — `app/routes/dashboard.py:dashboard_recent_battles`

Replace the existing card-list builder. New shape:

```python
def _band_meta(label: str) -> tuple[str, str]:
    """Return (band_short, css_class) for a group_label.
    K-space → NS/LS/HS; WH classes → keep label (C5, Thera, Pochven…)."""
    if label == "Nullsec": return ("NS", "rb-band-ns")
    if label == "Lowsec":  return ("LS", "rb-band-ls")
    if label == "Highsec": return ("HS", "rb-band-hs")
    if label == "C13 (Shattered)": return ("C13", "rb-band-wh")
    return (label, "rb-band-wh")

# Walk groups once; annotate each battle dict with band info and accumulate
# the rollup as we go. Single pass, no extra DB hits.
rollup: list[dict] = []
all_battles: list[dict] = []
for label, battles in groups.items():
    if not battles:
        continue
    short, css = _band_meta(label)
    total_kills = sum(b["kill_count"] or 0 for b in battles)
    total_isk = sum(b["total_isk"] or 0 for b in battles)
    top = max(battles, key=lambda b: b["kill_count"] or 0)
    for b in battles:
        b["band_short"] = short
        b["band_class"] = css
        all_battles.append(b)
    rollup.append({
        "band_short": short,
        "band_class": css,
        "fight_count": len(battles),
        "total_kills": total_kills,
        "total_isk": total_isk,
        "top_system_id": top["system_id"],
        "top_system_name": top["system_name"] or f"#{top['system_id']}",
        "top_attacker_label": top.get("attacker_label") or "",
        "top_victim_label": top.get("victim_label") or "",
    })

rollup.sort(key=lambda r: r["total_kills"], reverse=True)
top10 = sorted(all_battles, key=lambda b: b["kill_count"] or 0, reverse=True)[:10]

return templates.TemplateResponse(
    request, "partials/dashboard_recent_battles.html",
    {"rollup": rollup, "top10": top10},
)
```

The `SEC_BAND_ORDER` import goes away — no longer needed.

### Frontend — `app/templates/partials/dashboard_recent_battles.html`

Rewrite. Grid container uses `minmax(0, 1fr)` to prevent track-blowout. Each row is a grid of fixed badge + flexible body + nowrap stat; the flexible body has `min-width:0` so its overflow can ellipsis instead of expanding.

```html
{# Two-column recent-battles widget.
   Left: per-band rollup, sorted by total kills DESC.
   Right: flat top-10 by kill_count across all bands.
   Both columns use minmax(0, 1fr) tracks so long content cannot blow
   past the 1360px parent. Stacks vertically below 900px. #}
<style nonce="{{ request.state.csp_nonce }}">
  .rb-grid { display:grid; grid-template-columns: minmax(0, 3fr) minmax(0, 2fr); gap:1rem; }
  @media (max-width: 900px) { .rb-grid { grid-template-columns: minmax(0, 1fr); } }

  .rb-col-head { font-size:9px; color:var(--muted); letter-spacing:0.15em; text-transform:uppercase; margin:0 0 0.4rem; padding:0 0.5rem; }

  .rb-row { display:grid; align-items:center; gap:0.5rem; padding:6px 8px; text-decoration:none; color:inherit; border-bottom:1px solid var(--border); font-size:11px; background:var(--bg-raised, #111); }
  .rb-row:hover { background:rgba(255,255,255,0.05); }
  .rb-row:last-of-type { border-bottom:none; }

  /* rollup row: badge (42px) · body (flex) · stat (auto) */
  .rb-rollup-row { grid-template-columns: 42px minmax(0, 1fr) auto; }
  /* top10 row: rank (20px) · badge (42px) · body (flex) · stat (auto) */
  .rb-top10-row { grid-template-columns: 20px 42px minmax(0, 1fr) auto; }

  .rb-band-badge { font-size:9px; letter-spacing:0.1em; padding:2px 0; text-transform:uppercase; text-align:center; font-family:monospace; font-weight:bold; border-radius:2px; }
  .rb-band-hs { background:#1a4d2e; color:#7fd99a; }
  .rb-band-ls { background:#5a3a1a; color:#e0a85a; }
  .rb-band-ns { background:#5a1a1a; color:#e07a7a; }
  .rb-band-wh { background:#2a1a4d; color:#a78bfa; }

  .rb-rank { color:var(--muted); font-family:monospace; font-size:10px; text-align:right; }
  .rb-body { min-width:0; }
  .rb-sys { color:var(--fg); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; display:block; }
  .rb-meta { color:var(--muted); font-size:9px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; display:block; }
  .rb-stat { color:var(--accent); font-family:monospace; font-size:10px; white-space:nowrap; text-align:right; }
</style>

<div class="b-section">
  <div class="b-section-head">
    <span class="b-label">Major Fleet Battles in New Eden — last 7 days</span>
  </div>
  <div class="b-pad-md">
    {% if not rollup and not top10 %}
    <div style="font-size:11px;color:var(--muted);">No battles in the last 7 days. Discovery runs every 15 min.</div>
    {% else %}
    <div class="rb-grid">
      <div>
        <div class="rb-col-head">Band activity</div>
        {% for r in rollup %}
        <a class="rb-row rb-rollup-row"
           href="https://zkillboard.com/system/{{ r['top_system_id'] }}/"
           target="_blank" rel="noopener"
           title="{{ r['top_attacker_label'] or '—' }} vs {{ r['top_victim_label'] or '—' }}">
          <span class="rb-band-badge {{ r['band_class'] }}">{{ r['band_short'] }}</span>
          <span class="rb-body">
            <span class="rb-sys">{{ r['top_system_name'] }} <span style="color:var(--muted);font-size:9px;">{{ r['fight_count'] }}F</span></span>
            <span class="rb-meta">{{ r['top_attacker_label'] or '—' }} vs {{ r['top_victim_label'] or '—' }}</span>
          </span>
          <span class="rb-stat">{{ r['total_kills'] }}K{% if r['total_isk'] and r['total_isk'] > 0 %} · {{ (r['total_isk']/1e9)|round(1) }}B{% endif %}</span>
        </a>
        {% endfor %}
      </div>
      <div>
        <div class="rb-col-head">Top 10 by kills</div>
        {% for b in top10 %}
        <a class="rb-row rb-top10-row"
           href="https://zkillboard.com/system/{{ b['system_id'] }}/"
           target="_blank" rel="noopener"
           title="{{ b['attacker_label'] or '—' }} vs {{ b['victim_label'] or '—' }}">
          <span class="rb-rank">{{ loop.index }}</span>
          <span class="rb-band-badge {{ b['band_class'] }}">{{ b['band_short'] }}</span>
          <span class="rb-body">
            <span class="rb-sys">{{ b['system_name'] or b['system_id'] }}</span>
            <span class="rb-meta">{{ b['attacker_label'] or '—' }} vs {{ b['victim_label'] or '—' }}</span>
          </span>
          <span class="rb-stat">{{ b['kill_count'] }}K · {{ b['pilots_involved'] }}P</span>
        </a>
        {% endfor %}
      </div>
    </div>
    {% endif %}
  </div>
</div>
```

## Behavior

- **Rollup row click** — opens the band's *top* fight's system on zKillboard (the highest kill_count system in that band).
- **Top-10 row click** — opens that exact fight's system on zKillboard.
- **Hover** — `title` attribute shows the full attacker-vs-victim labels even when ellipsis truncates them.
- **Empty groups** — skipped silently. If a band has zero battles in the 7-day window it doesn't appear in either column.
- **No battles at all** — single muted "No battles in the last 7 days" message (existing fallback).

## Edge cases

- **WH classes vs K-space ISK** — `total_isk` is often 0 or NULL on WH battles (zKB seldom prices wormhole losses in DetectedBattle.total_isk). The ISK fragment in the rollup row is gated by `{% if r['total_isk'] and r['total_isk'] > 0 %}` so we don't show `· 0.0B`.
- **Long attacker/victim labels** — wrapped in `.rb-meta` which has `overflow:hidden; text-overflow:ellipsis; white-space:nowrap`. The badge and stat columns have fixed widths so they're never starved.
- **Top-10 has fewer than 10 fights** — `loop.index` keeps the rank correct; missing entries just don't render.
- **C13 (Shattered) label** — collapses to `C13` in the badge to fit the 42px badge slot.
- **Very wide viewport** — extra space distributes across the two `minmax(0, 1fr)` tracks proportionally. No empty whitespace gap on the right because both columns fill.
- **Mobile / narrow** — `@media (max-width: 900px)` collapses to single column; left rollup renders first, then top-10 below.

## Testing

Manual on production after deploy. Visual sanity checks:

1. Dashboard loads with the widget visible above the fold; no horizontal scroll, no card running off-screen.
2. NS / LS / HS rows present in the rollup, colored, ordered by total kills.
3. Top-10 shows ranked entries with band badges and `kills · pilots` stat right-aligned.
4. Hover a row — title tooltip shows the full attacker-vs-victim labels.
5. Click any row — opens correct zKillboard system page in a new tab.
6. Resize narrow (< 900px) — columns stack vertically, content still readable.

No new automated tests — pure UI + display-format change on existing data shapes. The DB queries are unchanged.

## Rollback

Three files in scope (`app/routes/dashboard.py`, `app/templates/partials/dashboard_recent_battles.html`, and the spec). `git revert` if needed. Low blast radius — single dashboard widget, no schema/data change.
