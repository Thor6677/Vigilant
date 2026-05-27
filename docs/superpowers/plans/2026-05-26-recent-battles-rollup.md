# Recent Battles Rollup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the overflow-prone card grid on the dashboard "Major Fleet Battles" widget with a two-column rollup + top-10 layout that never overflows and matches DOTLAN/zKillboard dense-leaderboard patterns.

**Architecture:** Pure UI/aggregation change — `app/routes/dashboard.py` builds two lists (per-band rollup, flat top-10) from the existing `query_battles_window` output; `app/templates/partials/dashboard_recent_battles.html` renders them in a two-column CSS Grid with `minmax(0, 1fr)` tracks. No DB schema change, no new queries.

**Tech Stack:** FastAPI/Jinja2 template, vanilla CSS Grid, no new dependencies.

**Spec:** `docs/superpowers/specs/2026-05-26-recent-battles-rollup-design.md`

---

### Task 1: Rebuild route aggregation + template

**Goal:** `/dashboard/recent-battles` returns a band-rollup list and a top-10 list to the template; the template renders them in a two-column grid with overflow-safe tracks.

**Files:**
- Modify: `app/routes/dashboard.py` — `dashboard_recent_battles` function (currently ~lines 2231–2252)
- Modify: `app/templates/partials/dashboard_recent_battles.html` — full rewrite

**Acceptance Criteria:**
- [ ] Dashboard widget renders without horizontal overflow on a viewport that previously clipped cards.
- [ ] Left column shows one row per band that has battles, sorted by total kills DESC; row contains a colored band badge, top fight's system name, fight count, attacker-vs-victim line, and `<total_kills>K · <total_isk_b>B` stat.
- [ ] Right column shows up to 10 ranked rows across all bands by `kill_count` DESC; each row has rank, band badge, system name, attacker-vs-victim line, and `<kill_count>K · <pilots>P` stat.
- [ ] Empty groups skipped silently; "No battles in the last 7 days" message still shown when both lists are empty.
- [ ] Each row is an `<a>` to `https://zkillboard.com/system/<id>/` opening in a new tab.
- [ ] `title` attribute on each row exposes the full attacker-vs-victim labels for the hover tooltip.
- [ ] At viewport width < 900px, columns stack vertically.

**Verify:** Manual on production after deploy — see spec §Testing. No automated tests.

**Steps:**

- [ ] **Step 1: Rewrite the route**

Replace `dashboard_recent_battles` in `app/routes/dashboard.py` (currently ~lines 2231–2252) with this implementation. The function name, route, and signature stay the same; only the body changes.

Find:

```python
@router.get("/dashboard/recent-battles", response_class=HTMLResponse)
async def dashboard_recent_battles(request: Request, db: AsyncSession = Depends(get_db)):
    from app.config import get_settings as _gs
    cfg = _gs()
    if not (cfg.killmails_enabled and cfg.killmail_battles_enabled):
        return HTMLResponse("")
    from app.intel.recent_battles import query_battles_window
    groups = await query_battles_window(days=7)
    # Single unified card grid: K-space first (most active in fleet terms),
    # then WH classes. Each entry is (label, color-hint, top-2 battles).
    # color-hint is the band short-code for K-space cards (hs/ls/ns); None
    # for WH classes (uses the default muted label color).
    cards: list[tuple[str, str | None, list]] = []
    kspace_order = [("NS", "ns", "Nullsec"), ("LS", "ls", "Lowsec"), ("HS", "hs", "Highsec")]
    for short, color, full in kspace_order:
        if groups.get(full):
            cards.append((short, color, groups[full][:2]))
    wh_order = ["C1", "C2", "C3", "C4", "C5", "C6", "Thera", "C13 (Shattered)", "Drifter", "Pochven"]
    for k in wh_order:
        if groups.get(k):
            cards.append((k, None, groups[k][:2]))
    return templates.TemplateResponse(
        request, "partials/dashboard_recent_battles.html", {"cards": cards}
    )
```

Replace with:

```python
@router.get("/dashboard/recent-battles", response_class=HTMLResponse)
async def dashboard_recent_battles(request: Request, db: AsyncSession = Depends(get_db)):
    from app.config import get_settings as _gs
    cfg = _gs()
    if not (cfg.killmails_enabled and cfg.killmail_battles_enabled):
        return HTMLResponse("")
    from app.intel.recent_battles import query_battles_window
    groups = await query_battles_window(days=7)

    def _band_meta(label: str) -> tuple[str, str]:
        """Map a group_label to (short_code, badge_css_class).
        K-space gets its 2-char code + band-tinted badge; every WH class
        keeps its label (collapsed to fit the badge) on the shared purple
        WH tint."""
        if label == "Nullsec":
            return ("NS", "rb-band-ns")
        if label == "Lowsec":
            return ("LS", "rb-band-ls")
        if label == "Highsec":
            return ("HS", "rb-band-hs")
        if label == "C13 (Shattered)":
            return ("C13", "rb-band-wh")
        return (label, "rb-band-wh")

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

- [ ] **Step 2: Rewrite the template**

Replace the entire contents of `app/templates/partials/dashboard_recent_battles.html` with this. The file is currently the card-grid version from commit c9795f5.

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
  .rb-fightcount { color:var(--muted); font-size:9px; margin-left:4px; }
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
            <span class="rb-sys">{{ r['top_system_name'] }}<span class="rb-fightcount">{{ r['fight_count'] }}F</span></span>
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

- [ ] **Step 3: Syntax check the modified Python**

Run:

```bash
python3 -c "import ast; ast.parse(open('app/routes/dashboard.py').read()); print('dashboard.py OK')"
```

Expected: `dashboard.py OK`

If this fails, fix the indentation / syntax in dashboard.py before continuing.

- [ ] **Step 4: Sanity-grep the inserted symbols**

Run:

```bash
echo "rollup refs: $(grep -c "rollup" app/routes/dashboard.py)"
echo "top10 refs: $(grep -c "top10" app/routes/dashboard.py)"
echo "rb-rollup-row markup: $(grep -c 'rb-rollup-row' app/templates/partials/dashboard_recent_battles.html)"
echo "rb-top10-row markup: $(grep -c 'rb-top10-row' app/templates/partials/dashboard_recent_battles.html)"
echo "rb-band-ns / ls / hs / wh: $(grep -cE 'rb-band-(ns|ls|hs|wh)' app/templates/partials/dashboard_recent_battles.html)"
echo "minmax(0, 1fr): $(grep -c 'minmax(0,' app/templates/partials/dashboard_recent_battles.html)"
```

Expected (≥ values, never less):

- `rollup refs: 7` (declaration + 5 appends/uses + sort + return)
- `top10 refs: 2` (build + return)
- `rb-rollup-row markup: 2` (CSS + template)
- `rb-top10-row markup: 2` (CSS + template)
- `rb-band-ns / ls / hs / wh: 4` (one CSS rule per class)
- `minmax(0, 1fr): 4` (rb-grid 3fr + 2fr + responsive 1fr + body 1fr in rollup + body 1fr in top10)

If any count is materially lower, an insertion got missed — re-read the affected step.

- [ ] **Step 5: Commit**

```bash
git add app/routes/dashboard.py app/templates/partials/dashboard_recent_battles.html
git commit -m "feat(dashboard): two-column rollup + top-10 for recent battles

Card grid blew past the 1360px container because grid 1fr is
minmax(auto, 1fr) and white-space:nowrap content can blow out the
auto min. Rewrite the widget as a two-column layout with minmax(0,
1fr) tracks (no blow-out possible):

- Left: per-band rollup, one row per band that has battles, sorted
  by total kills DESC. Row = badge + top system + matchup + stat.
- Right: flat top-10 by kill_count across all bands. Row = rank +
  badge + system + matchup + stat.

Both columns use the existing band palette (NS red / LS amber / HS
green / WH purple). Stacks vertically below 900px. No new DB
queries — same query_battles_window output, aggregated in the
route.

Spec: docs/superpowers/specs/2026-05-26-recent-battles-rollup-design.md

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

- [ ] **Step 6: Deploy to VPS**

Run:

```bash
git push origin main && ssh ijohnson@146.190.140.112 "/opt/vigilant/scripts/deploy.sh" 2>&1 | tail -5
```

Wait for `✓ Deploy complete. Running commit: <sha>`.

- [ ] **Step 7: Manual verification on production**

Open `https://vigilant.thunderborn.dev/dashboard` in a hard-refreshed browser tab. Run through these visual checks:

1. The "Major Fleet Battles in New Eden — last 7 days" widget renders with two columns side-by-side (rollup left, top-10 right).
2. No horizontal scroll on the page; the right column doesn't get clipped by the viewport edge.
3. Rollup includes a row for every band that has battles in the last 7 days. NS / LS / HS rows appear when applicable, with their colored badges.
4. Rollup is sorted by total kills DESC (most active band on top).
5. Top-10 list shows ranked rows (`1` through up to `10`) with band badges and `kills · pilots` stat.
6. Hovering any row shows the full attacker-vs-victim labels in the title tooltip (visible even when the visible text is ellipsis-truncated).
7. Clicking any row opens the correct zKillboard system page in a new tab.
8. Resize the window narrow (< 900px wide). The two columns stack vertically; content is still readable.
9. (If currently empty) verify that with no battles in 7 days, the muted "No battles in the last 7 days" fallback shows.

If any check fails, the bug is localized to the route or template — fix and re-deploy. No backend/data rollback needed.

---

## Self-Review Notes

Ran the spec § by § against this plan:

- **§Goal / §Why this shape** — covered by Task 1 Goal and Architecture.
- **§Non-goals** — honored: no drill-down (Step 2 templates just have `<a href>`); no new queries (Step 1 reuses `query_battles_window`); no animations.
- **§Layout** — the mock matches the template in Step 2 (two columns, badges, system + matchup + stat per row).
- **§Components — Backend** — Step 1 contains the exact `_band_meta` helper and rollup/top10 construction as specified.
- **§Components — Frontend** — Step 2 contains the full template (style block + markup) with `minmax(0, 3fr) minmax(0, 2fr)`, the 4 band classes, and the responsive stack below 900px.
- **§Behavior** — Step 7 verification checks the row-click → zKB, the title hover, and the empty-state fallback.
- **§Edge cases** — handled inline in template (ISK gated by `> 0`, badge labels collapsed for C13, `rb-meta` ellipsis on long matchups).
- **§Testing** — Step 7 enumerates the manual visual checks; spec said "no automated tests" and the plan matches.
- **§Rollback** — `git revert` of the single feature commit, called out in the route-comment style by the commit message.

No placeholders. Symbol consistency: `rollup` / `top10` / `_band_meta` / `rb-band-ns/ls/hs/wh` / `rb-rollup-row` / `rb-top10-row` all match between Step 1 and Step 2.
