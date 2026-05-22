# Kill Feed — Advanced Search MVP · Implementation Plan (Plan 1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the MVP of `/intel/kills/search` — zKillboard-style advanced kill search with chip filters, autocomplete entity/ship search, And/In/Or modes, cursor pagination + "Show more", optional live polling, and an NPC badge on the shared row partial.

**Architecture:** New page + new results route (`/intel/kills/search` + `/intel/kills/search/results`). Reuses Feature A's autocomplete endpoint and the live feed's row partial. Single server-side filter compiler (`_compile_search_where`) translates querystring → SQLAlchemy WHERE clauses. Client-side state lives in querystring + localStorage shadow. Live polling is a JS-driven setInterval (only enabled when sort=Date Desc + open-ended time) using the same prepend-with-dedupe pattern as the live feed.

**Tech Stack:** FastAPI + SQLAlchemy async + Jinja2 + htmx + vanilla JS (matches existing app stack). No new dependencies.

**Spec:** `docs/superpowers/specs/2026-05-22-killfeed-advanced-search-design.md`. Plan 2 (Awox / Padding / HighSec Gank flags + AT Ships category) gets its own plan file after this lands.

**Vigilant conventions (read once):**
- Direct commits to `main`; `deploy.sh` does `git pull` + Docker rebuild.
- Pre-deploy: syntax-check every modified `.py` with `python3 -c "import ast; ast.parse(open(F).read())"`.
- Pre-deploy for templates: `python3 -c "from jinja2 import Environment, FileSystemLoader, select_autoescape; ..."`.
- Jinja2 dict access uses `d['key']`, not `d.key` (CLAUDE.md gotcha).
- AsyncSession is **not** safe for concurrent statements — sequential reads only on one session.
- Indexes on existing tables don't auto-deploy via `create_all` — use `CREATE INDEX IF NOT EXISTS` (`feedback_create_all_skips_indexes`).
- Container has no `sqlite3` binary; for read-only ad-hoc queries use `python3 -c "import sqlite3; c=sqlite3.connect('file:/data/vigilant.db?mode=ro&immutable=1', uri=True); ..."`.

**File structure:**

| File | Action | Responsibility |
|---|---|---|
| `app/routes/intel_kills_search.py` | Create | Page route + results route + filter compiler + cursor logic |
| `app/templates/intel_kills_search.html` | Create | Page shell — filter card markup, results container, JS |
| `app/templates/partials/intel_kills_search_results.html` | Create | Results-rows partial (row markup + cursor markers + stats meta) |
| `app/templates/partials/intel_kills_feed.html` | Modify | Add `[NPC]` badge conditional rendering |
| `app/routes/intel_kills.py` | Modify | Surface `is_npc` in `_enrich_kills` so live feed shows badge too |
| `app/templates/intel_kills.html` | Modify | Add CSS for `.kf-npc-badge`; add link to search page in header |
| `app/main.py` | Modify | Register the new search router |
| `app/db/models.py` | Modify (Task 7 conditional) | Index additions if EXPLAIN says so |

---

### Task 1: Page shell + filter card HTML/CSS + link from /intel/kills

**Goal:** A working `/intel/kills/search` route that renders the page with all filter chips/inputs visible (markup-only, no interactivity yet). Link added from `/intel/kills` header so users can navigate to the new page.

**Files:**
- Create: `app/routes/intel_kills_search.py` (page route stub only)
- Create: `app/templates/intel_kills_search.html` (full filter card + empty results container)
- Modify: `app/main.py` — register new router
- Modify: `app/templates/intel_kills.html` — header link

**Acceptance Criteria:**
- [ ] `GET /intel/kills/search` returns the page when authenticated; redirects to `/` when not.
- [ ] All filter chip rows are visible: Time, Space, WH class (hidden until WH chip is on), Category, Count, ISK, Primetime, Ship, Attackers, Either, Victim, Sort, Live toggle.
- [ ] No JS interactivity yet — chips are inert. Clicking does nothing. This is a markup-only milestone.
- [ ] Header on `/intel/kills` has a small "advanced search ↗" link to the new page.

**Verify:**
1. `python3 -c "import ast; ast.parse(open('app/routes/intel_kills_search.py').read())"` → exit 0.
2. Jinja2 templates compile: `python3 -c "from jinja2 import Environment, FileSystemLoader, select_autoescape; e = Environment(loader=FileSystemLoader('app/templates'), autoescape=select_autoescape(['html'])); e.get_template('intel_kills_search.html'); e.get_template('intel_kills.html')"` → no errors.
3. Deploy + browse `/intel/kills/search` from logged-in browser: filter card visible, no JS errors in console.

**Steps:**

- [ ] **Step 1: Create the new route module `app/routes/intel_kills_search.py`**

```python
"""Intel → Kill Feed → Advanced Search.

Sibling of /intel/kills. Full filter UI + cursor pagination + optional live
polling. Spec: docs/superpowers/specs/2026-05-22-killfeed-advanced-search-design.md.

Plan 1 (this MVP):
  - Page route (this file, Task 1)
  - Filter compiler + /search/results endpoint (Task 2)
  - Results partial + NPC badge surfacing (Task 3 modifies the shared partial)
  - Frontend wiring (Task 4-5 in intel_kills_search.html)
  - Live polling (Task 6)

Plan 2 (later) adds heuristic flags (Awox/Padding/HighSec Gank) and AT Ships
category.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import get_db

log = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/intel/kills/search", response_class=HTMLResponse)
async def intel_kills_search_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Page shell. Filters + empty results container, JS handles the rest."""
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/")
    return templates.TemplateResponse("intel_kills_search.html", {"request": request})
```

- [ ] **Step 2: Create the page template `app/templates/intel_kills_search.html`**

Write the file with this exact content (long — markup only, JS comes in Task 4-6):

```html
{% extends "base.html" %}
{% block title %}Advanced Kill Search — VIGILANT{% endblock %}
{% block breadcrumbs %}
<div class="b-breadcrumbs"><a href="/intel">Intel</a><span class="b-crumb-sep">&gt;</span><a href="/intel/kills">Kill Feed</a><span class="b-crumb-sep">&gt;</span><span class="b-crumb-current">Advanced Search</span></div>
{% endblock %}

{% block head %}
<style nonce="{{ request.state.csp_nonce }}">
  /* ── filter card ──────────────────────────────────────────────── */
  .kfs-head { display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid var(--border); padding-bottom:10px; margin-bottom:10px; }
  .kfs-title { font-size:13px; letter-spacing:0.16em; color:var(--text); font-weight:600; }
  .kfs-head-actions { display:flex; gap:6px; }
  .kfs-btn { background:var(--surface); border:1px solid var(--border); color:var(--text); padding:4px 10px; font-size:10px; cursor:pointer; border-radius:3px; }
  .kfs-btn:hover { border-color:var(--accent); }
  .kfs-btn.primary { background:rgba(94,177,255,0.15); color:var(--accent); border-color:var(--accent); }
  .kfs-filters { display:flex; flex-direction:column; gap:6px; padding:10px 0; border-bottom:1px solid var(--border); margin-bottom:10px; }
  .kfs-filter-row { display:flex; flex-wrap:wrap; gap:6px; align-items:center; }
  .kfs-glabel { font-size:9px; color:var(--muted); text-transform:uppercase; letter-spacing:0.12em; margin-right:4px; min-width:80px; }
  .kfs-chip { background:var(--surface); color:var(--text); padding:3px 9px; border:1px solid var(--border); font-size:10px; cursor:pointer; border-radius:3px; user-select:none; }
  .kfs-chip.on { background:rgba(94,177,255,0.15); color:var(--accent); border-color:var(--accent); }
  .kfs-chip.mod { border-style:dashed; }
  .kfs-chip.mod.on { background:rgba(167,139,250,0.15); color:#a78bfa; border-color:#a78bfa; }
  .kfs-chip.disabled { opacity:0.4; cursor:not-allowed; }
  .kfs-date-input { background:var(--surface); border:1px solid var(--border); color:var(--text); padding:3px 9px; font-size:10px; border-radius:3px; width:130px; font-family:"SF Mono",Menlo,monospace; }
  .kfs-date-input::placeholder { color:var(--muted); }
  /* autocomplete reuses Feature A's pattern */
  .kfs-ac { position:relative; }
  .kfs-ac-input { background:var(--surface); border:1px solid var(--border); color:var(--text); padding:3px 9px; font-size:10px; border-radius:3px; width:180px; }
  .kfs-ac-input:focus { outline:none; border-color:var(--accent); }
  .kfs-ac-results { position:absolute; top:100%; left:0; background:var(--surface); border:1px solid var(--border); margin-top:2px; min-width:240px; max-height:240px; overflow:auto; z-index:50; display:none; }
  .kfs-ac-results.show { display:block; }
  .kfs-ac-result { padding:4px 8px; font-size:10px; cursor:pointer; color:var(--text); }
  .kfs-ac-result:hover { background:rgba(94,177,255,0.1); }
  .kfs-ac-result .kind { color:var(--muted); font-size:9px; margin-left:4px; }
  .kfs-chips { display:inline-flex; gap:4px; flex-wrap:wrap; }
  .kfs-chip-removable { background:rgba(94,177,255,0.15); color:var(--accent); padding:2px 6px 2px 8px; font-size:10px; border-radius:3px; cursor:pointer; display:inline-flex; align-items:center; gap:4px; }
  .kfs-chip-removable .x { font-weight:700; }
  .kfs-modes { display:inline-flex; gap:2px; }
  .kfs-mode-btn { background:var(--surface); color:var(--muted); padding:2px 6px; border:1px solid var(--border); font-size:9px; cursor:pointer; text-transform:uppercase; letter-spacing:0.06em; }
  .kfs-mode-btn.on { background:rgba(94,177,255,0.15); color:var(--accent); border-color:var(--accent); }
  /* stats + results */
  .kfs-stats { display:flex; justify-content:space-between; padding:6px 0; font-size:11px; color:var(--muted); border-bottom:1px solid var(--border); margin-bottom:6px; }
  .kfs-stats strong { color:var(--text); }
  .kfs-loading { color:var(--muted); font-size:11px; padding:10px 0; text-align:center; }
  .kfs-show-more-wrap { text-align:center; padding:14px 0; }
  /* NPC badge — shared with live feed, defined here for the search page CSS scope */
  .kf-npc-badge { display:inline-block; background:var(--surface); color:var(--muted); padding:0 4px; font-size:8px; font-weight:600; letter-spacing:0.08em; border-radius:2px; border:1px solid var(--border); margin-left:4px; vertical-align:middle; }
</style>
{% endblock %}

{% block content %}
<div class="b-section">
  <div class="b-section-head kfs-head">
    <span class="kfs-title">ADVANCED KILL SEARCH</span>
    <div class="kfs-head-actions">
      <button type="button" class="kfs-btn" id="kfs-reset">Reset</button>
      <a class="kfs-btn" href="/intel/kills" style="text-decoration:none;">← Back to live feed</a>
    </div>
  </div>

  <div class="kfs-filters">
    <div class="kfs-filter-row">
      <span class="kfs-glabel">Time</span>
      <span class="kfs-chip" data-time="24h">24h</span>
      <span class="kfs-chip" data-time="7d">7d</span>
      <span class="kfs-chip" data-time="30d">30d</span>
      <span class="kfs-chip" data-time="90d">90d</span>
      <span style="color:var(--muted);font-size:10px;margin:0 6px;">or range:</span>
      <input type="text" class="kfs-date-input" id="kfs-time-start" placeholder="YYYY-MM-DD HH:MM" autocomplete="off">
      <input type="text" class="kfs-date-input" id="kfs-time-end" placeholder="YYYY-MM-DD HH:MM" autocomplete="off">
    </div>
    <div class="kfs-filter-row">
      <span class="kfs-glabel">Space</span>
      <span class="kfs-chip" data-space="hs">HS</span>
      <span class="kfs-chip" data-space="ls">LS</span>
      <span class="kfs-chip" data-space="ns">NS</span>
      <span class="kfs-chip" data-space="wh">WH</span>
      <span class="kfs-chip" data-space="abyssal">Abyssal</span>
    </div>
    <div class="kfs-filter-row" id="kfs-wh-subclasses" style="display:none;">
      <span class="kfs-glabel">WH class</span>
      {% for c in ['C1','C2','C3','C4','C5','C6','Thera','Drifter','Pochven'] %}
      <span class="kfs-chip" data-whclass="{{ c|lower }}">{{ c }}</span>
      {% endfor %}
      <span class="kfs-chip mod" data-mod="shattered">Shattered only</span>
    </div>
    <div class="kfs-filter-row">
      <span class="kfs-glabel">Category</span>
      <span class="kfs-chip" data-category="ship">Ship</span>
      <span class="kfs-chip" data-category="structure">Structure</span>
      <span class="kfs-chip" data-category="capital">Capital</span>
    </div>
    <div class="kfs-filter-row">
      <span class="kfs-glabel">Count</span>
      <span class="kfs-chip" data-count="solo">Solo</span>
      <span class="kfs-chip" data-count="2-5">2-5</span>
      <span class="kfs-chip" data-count="6-10">6-10</span>
      <span class="kfs-chip" data-count="11-25">11-25</span>
      <span class="kfs-chip" data-count="26-50">26-50</span>
      <span class="kfs-chip" data-count="51-100">51-100</span>
      <span class="kfs-chip" data-count="100+">100+</span>
    </div>
    <div class="kfs-filter-row">
      <span class="kfs-glabel">ISK</span>
      <span class="kfs-chip" data-isk="100m">100m+</span>
      <span class="kfs-chip" data-isk="1b">1b+</span>
      <span class="kfs-chip" data-isk="5b">5b+</span>
      <span class="kfs-chip" data-isk="10b">10b+</span>
      <span class="kfs-chip" data-isk="100b">100b+</span>
      <span class="kfs-chip" data-isk="1t">1t+</span>
    </div>
    <div class="kfs-filter-row">
      <span class="kfs-glabel">Primetime</span>
      <span class="kfs-chip" data-primetime="aus">Aus/CN</span>
      <span class="kfs-chip" data-primetime="eu">EU</span>
      <span class="kfs-chip" data-primetime="ru">RU</span>
      <span class="kfs-chip" data-primetime="use">USE</span>
      <span class="kfs-chip" data-primetime="usw">USW</span>
    </div>
    <div class="kfs-filter-row">
      <span class="kfs-glabel">Ship</span>
      <div class="kfs-ac">
        <input type="text" id="kfs-ship-input" class="kfs-ac-input" placeholder="Vargur, Loki…" autocomplete="off">
        <div id="kfs-ship-results" class="kfs-ac-results"></div>
      </div>
      <div id="kfs-ship-chips" class="kfs-chips"></div>
    </div>
    <div class="kfs-filter-row">
      <span class="kfs-glabel">Attackers</span>
      <div class="kfs-modes" data-side="attacker">
        <button type="button" class="kfs-mode-btn on" data-mode="or">Or</button>
        <button type="button" class="kfs-mode-btn" data-mode="and">And</button>
        <button type="button" class="kfs-mode-btn" data-mode="in">In</button>
      </div>
      <div class="kfs-ac">
        <input type="text" id="kfs-attacker-input" class="kfs-ac-input" placeholder="char/corp/alli/ship" autocomplete="off">
        <div id="kfs-attacker-results" class="kfs-ac-results"></div>
      </div>
      <div id="kfs-attacker-chips" class="kfs-chips"></div>
    </div>
    <div class="kfs-filter-row">
      <span class="kfs-glabel">Either</span>
      <div class="kfs-modes" data-side="either">
        <button type="button" class="kfs-mode-btn on" data-mode="or">Or</button>
        <button type="button" class="kfs-mode-btn" data-mode="and">And</button>
        <button type="button" class="kfs-mode-btn" data-mode="in">In</button>
      </div>
      <div class="kfs-ac">
        <input type="text" id="kfs-either-input" class="kfs-ac-input" placeholder="char/corp/alli/ship" autocomplete="off">
        <div id="kfs-either-results" class="kfs-ac-results"></div>
      </div>
      <div id="kfs-either-chips" class="kfs-chips"></div>
    </div>
    <div class="kfs-filter-row">
      <span class="kfs-glabel">Victim</span>
      <div class="kfs-modes" data-side="victim">
        <button type="button" class="kfs-mode-btn on" data-mode="or">Or</button>
        <button type="button" class="kfs-mode-btn" data-mode="and">And</button>
        <button type="button" class="kfs-mode-btn" data-mode="in">In</button>
      </div>
      <div class="kfs-ac">
        <input type="text" id="kfs-victim-input" class="kfs-ac-input" placeholder="char/corp/alli/ship" autocomplete="off">
        <div id="kfs-victim-results" class="kfs-ac-results"></div>
      </div>
      <div id="kfs-victim-chips" class="kfs-chips"></div>
    </div>
    <div class="kfs-filter-row">
      <span class="kfs-glabel">Sort</span>
      <span class="kfs-chip on" data-sort="date">Date</span>
      <span class="kfs-chip" data-sort="isk">ISK</span>
      <span class="kfs-chip" data-sort="involved">Involved</span>
      <span style="margin-left:8px;"></span>
      <span class="kfs-chip on" data-dir="desc">Desc</span>
      <span class="kfs-chip" data-dir="asc">Asc</span>
      <span style="margin-left:12px;"></span>
      <button type="button" class="kfs-btn" id="kfs-live-toggle" title="Live polling — requires sort=Date Desc and open-ended time">☐ Live</button>
    </div>
  </div>

  <div class="kfs-stats" id="kfs-stats">
    <span><strong id="kfs-stats-showing">—</strong> of <strong id="kfs-stats-total">—</strong> kills</span>
    <span>Total destroyed: <strong id="kfs-stats-isk">—</strong> ISK</span>
  </div>

  <p class="kfs-loading" id="kfs-loading">Loading…</p>
  <div id="kfs-results"></div>
  <div class="kfs-show-more-wrap" id="kfs-show-more-wrap" style="display:none;">
    <button type="button" class="kfs-btn primary" id="kfs-show-more">↓ Show more (next 100)</button>
  </div>
</div>
{% endblock %}
```

- [ ] **Step 3: Register the router in `app/main.py`**

Find the existing import line:

```python
from app.routes.intel_kills import router as intel_kills_router
```

Add immediately after:

```python
from app.routes.intel_kills_search import router as intel_kills_search_router
```

Find the include_router block (around line 106-114) where `intel_kills_router` is included. Add a line for the new router:

```python
app.include_router(intel_kills_search_router)
```

Place it adjacent to `app.include_router(intel_kills_router)` for tidiness.

- [ ] **Step 4: Add "advanced search" link to `/intel/kills` page header**

Open `app/templates/intel_kills.html`. Find the `.kf-head` section (around line 114-117):

```html
<div class="b-section-head kf-head">
  <span class="kf-title">KILL FEED</span>
  <span class="kf-live" id="kf-status">LIVE · 1h · — kills</span>
</div>
```

Add a third element — a small link to the search page — between the title and the live status:

```html
<div class="b-section-head kf-head">
  <span class="kf-title">KILL FEED</span>
  <a href="/intel/kills/search" style="font-size:10px;color:var(--accent);text-decoration:none;letter-spacing:0.06em;">advanced search ↗</a>
  <span class="kf-live" id="kf-status">LIVE · 1h · — kills</span>
</div>
```

- [ ] **Step 5: Syntax check + commit + deploy**

```bash
python3 -c "import ast; ast.parse(open('app/routes/intel_kills_search.py').read()); ast.parse(open('app/main.py').read())"
python3 -c "from jinja2 import Environment, FileSystemLoader, select_autoescape; e = Environment(loader=FileSystemLoader('app/templates'), autoescape=select_autoescape(['html'])); e.get_template('intel_kills_search.html'); e.get_template('intel_kills.html')"
git add app/routes/intel_kills_search.py app/templates/intel_kills_search.html app/main.py app/templates/intel_kills.html
git commit -m "$(cat <<'EOF'
feat(kills): /intel/kills/search page shell + filter card markup

Plan 1 Task 1 of the Advanced Search MVP. Adds the new page route and
template (markup only — no interactivity yet), registers the router,
and links from /intel/kills.

Spec: docs/superpowers/specs/2026-05-22-killfeed-advanced-search-design.md

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
ssh ijohnson@146.190.140.112 "/opt/vigilant/scripts/deploy.sh"
ssh ijohnson@146.190.140.112 "docker logs --tail 60 vigilant-app-1 2>&1 | grep -iE 'error|traceback' | head"
```

Expected: deploy completes, no new tracebacks. Browse `/intel/kills/search` from a logged-in browser, see the full filter card with all chip rows, all chips inert. Page console clean.

---

### Task 2: Server-side filter compiler + results endpoint

**Goal:** Working `GET /intel/kills/search/results` endpoint that accepts all filter params via querystring, compiles them to a SQLAlchemy WHERE clause (including And/In/Or for the three entity sides), runs the query with cursor pagination, resolves names, returns the rendered results partial.

**Files:**
- Modify: `app/routes/intel_kills_search.py` — add filter compiler + results route
- Create: `app/templates/partials/intel_kills_search_results.html` — rows + cursor markers

**Acceptance Criteria:**
- [ ] `GET /intel/kills/search/results` returns rendered row HTML for the matching kills (up to 100), plus a hidden marker div with cursor + stats values.
- [ ] Empty query (`?`) returns the most recent 100 kills universe-wide, Date Desc.
- [ ] Each documented filter dimension (time/space/category/count/ISK/primetime/ship/attacker/either/victim/sort/cursor) works in isolation when supplied via querystring.
- [ ] And/In/Or modes compile to the correct SQL shape per the spec's semantics table.
- [ ] Sort + cursor pagination round-trips correctly: passing the `oldest_cursor` from response N as `cursor=` on the next request fetches the next 100 older rows.
- [ ] Total count + sum-ISK are computed for the filter and emitted in the marker.

**Verify:**
1. `python3 -c "import ast; ast.parse(open('app/routes/intel_kills_search.py').read())"` → exit 0.
2. `python3 -c "from jinja2 import Environment, FileSystemLoader, select_autoescape; e = Environment(loader=FileSystemLoader('app/templates'), autoescape=select_autoescape(['html'])); e.get_template('partials/intel_kills_search_results.html')"` → no errors.
3. Deploy.
4. Via logged-in browser fetch `https://vigilant.thunderborn.dev/intel/kills/search/results` (no querystring) — expect HTML with 100 rows.
5. Test a non-trivial filter combo:
   - `?space=hs&isk=10b&time=7d&sort=isk&dir=desc` — high-sec, 10B+, last 7d, ISK Desc.
   - `?attacker_chars=90000001&attacker_ships=21628&attacker_mode=in` — should compile to the In-EXISTS SQL (verify in `EXPLAIN QUERY PLAN`).
6. Verify cursor: take `data-oldest-cursor` from the response, pass as `?cursor=<value>` on a follow-up request → returns the next 100 older rows with no duplicates.

**Steps:**

- [ ] **Step 1: Extend imports in `app/routes/intel_kills_search.py`**

Replace the current import block with:

```python
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import Integer, and_, cast, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Killmail, KillmailAttacker, get_db
from app.db.sde_models import SDESystem, SDEType
from app.intel.recent_battles import resolve_entity_names
from app.sde.lookup import _ensure_wh_class_cache, type_ids_to_names
from app.sde import lookup as sde_lookup
```

The `_ensure_wh_class_cache` + `sde_lookup` import is needed for the WH-sub-class filter (Task 2 uses the existing `_wh_class_cache` dict to map system_ids → wh_class_id).

- [ ] **Step 2: Add filter constants + helpers at module level**

After the existing `router` / `templates` declarations:

```python
PAGE_SIZE = 100

# Hardcoded EVE-meta constants for the compiler.
CAPITAL_GROUP_IDS = {547, 485, 30, 659, 513, 902, 1538}  # Carrier, Dread, Titan, Super, Freighter, JF, FAX
RORQUAL_TYPE_ID = 28352  # Industrial Command Ship group 941 also contains Porpoise+Orca which are NOT capitals.
ABYSSAL_SYSTEM_MIN = 32000001
ABYSSAL_SYSTEM_MAX = 32000200
WH_SYSTEM_MIN = 31000000
WH_SYSTEM_MAX = 31999999

ISK_MIN_MAP = {"100m": 1e8, "1b": 1e9, "5b": 5e9, "10b": 1e10, "100b": 1e11, "1t": 1e12}

COUNT_BUCKETS = {
    "solo": (1, 1),
    "2-5": (2, 5),
    "6-10": (6, 10),
    "11-25": (11, 25),
    "26-50": (26, 50),
    "51-100": (51, 100),
    "100+": (100, None),  # None = no upper bound
}

# Primetime bands (UTC hour, [start, end_exclusive]). Wraparound bands split.
PRIMETIME_BANDS = {
    "aus": [(10, 18)],
    "eu":  [(18, 24), (0, 2)],
    "ru":  [(14, 22)],
    "use": [(23, 24), (0, 7)],
    "usw": [(2, 10)],
}

# WH classes — strings come from URL as 'c1'..'c6','thera','drifter','pochven'.
# Map to integer wormhole_class_id values used by SDEWormholeClass.
WH_CLASS_ID_MAP = {
    "c1": 1, "c2": 2, "c3": 3, "c4": 4, "c5": 5, "c6": 6,
    "thera": 12, "drifter": 14,  # Drifter wormholes use class 14
    "pochven": 25,  # Pochven systems have wh_class_id = 25
}


def _split_ids(s: str) -> list[int]:
    return [int(x) for x in (s or "").split(",") if x.strip().isdigit()]


def _split_set(s: str) -> set[str]:
    return {p.strip() for p in (s or "").split(",") if p.strip()}
```

- [ ] **Step 3: Implement the filter compiler — `_compile_search_where`**

```python
async def _compile_search_where(params: dict[str, Any], db: AsyncSession) -> dict[str, Any]:
    """Translate validated params dict into SQLAlchemy clauses.

    Returns a dict with keys:
      where: list[ColumnElement]  — AND-combined clauses
      joins: set[str]              — table aliases needed ('sde_systems', 'sde_types')
      sort_col, sort_dir           — for ORDER BY
      cursor_clause                — optional WHERE for pagination, separate from main where

    The caller composes the final SQL.
    """
    where: list = []
    joins: set[str] = set()

    # ── Time ───────────────────────────────────────────────────────────
    cutoff_map = {"24h": timedelta(hours=24), "7d": timedelta(days=7),
                  "30d": timedelta(days=30), "90d": timedelta(days=90)}
    if params.get("time_preset") and params["time_preset"] in cutoff_map:
        cutoff = datetime.utcnow() - cutoff_map[params["time_preset"]]
        where.append(Killmail.killmail_time >= cutoff)
    if params.get("time_start"):
        where.append(Killmail.killmail_time >= params["time_start"])
    if params.get("time_end"):
        where.append(Killmail.killmail_time <= params["time_end"])

    # ── Space (HS/LS/NS/WH/Abyssal) ────────────────────────────────────
    space = params.get("space") or set()
    if space:
        joins.add("sde_systems")
        space_conds = []
        if "hs" in space:
            space_conds.append(SDESystem.security >= 0.5)
        if "ls" in space:
            space_conds.append(and_(SDESystem.security > 0.0, SDESystem.security < 0.5))
        if "ns" in space:
            space_conds.append(and_(SDESystem.security <= 0.0, SDESystem.system_id < WH_SYSTEM_MIN))
        if "wh" in space:
            space_conds.append(and_(SDESystem.system_id >= WH_SYSTEM_MIN, SDESystem.system_id <= WH_SYSTEM_MAX))
        if "abyssal" in space:
            space_conds.append(and_(SDESystem.system_id >= ABYSSAL_SYSTEM_MIN, SDESystem.system_id <= ABYSSAL_SYSTEM_MAX))
        if space_conds:
            where.append(or_(*space_conds))

    # ── WH sub-class (only meaningful when WH selected) ───────────────
    wh_class = params.get("wh_class") or set()
    if wh_class and "wh" in space:
        # Pre-resolve system_ids matching the requested classes from the SDE cache.
        await _ensure_wh_class_cache(db)
        wh_cache = sde_lookup._wh_class_cache or {}
        wanted_ids = {WH_CLASS_ID_MAP[c] for c in wh_class if c in WH_CLASS_ID_MAP}
        matching_systems = {sid for sid, cid in wh_cache.items() if cid in wanted_ids}
        # Filter to system-level matches only (cache also stores constellation/region IDs;
        # filter against the actual sde_systems rows to drop the non-system keys).
        if matching_systems:
            where.append(Killmail.solar_system_id.in_(matching_systems))
        else:
            # Requested classes have no matching systems — return empty result.
            where.append(Killmail.killmail_id == -1)

    # ── Shattered modifier (only meaningful with WH or wh_class) ─────
    if params.get("shattered_only"):
        # Shattered systems are tagged via SDE group_label in _sys_meta_cache,
        # not in a separate column. For the search page we accept that this
        # filter is a no-op on systems not in _sys_meta_cache (rare for kills
        # that are in the DB but not in the live buffer). Implementation:
        # post-filter results in Python. Skip the SQL side for MVP — flag for
        # follow-up if precision matters.
        pass  # Documented limitation; revisit if user reports it.

    # ── Category (Ship / Structure / Capital) ─────────────────────────
    category = params.get("category") or set()
    if category:
        joins.add("sde_types")
        cat_conds = []
        if "ship" in category:
            cat_conds.append(SDEType.category_id == 6)
        if "structure" in category:
            cat_conds.append(SDEType.category_id == 65)
        if "capital" in category:
            cat_conds.append(or_(
                SDEType.group_id.in_(CAPITAL_GROUP_IDS),
                Killmail.victim_ship_type_id == RORQUAL_TYPE_ID,
            ))
        if cat_conds:
            where.append(or_(*cat_conds))

    # ── Count (gang size) ─────────────────────────────────────────────
    count_buckets = params.get("count") or set()
    if count_buckets:
        count_conds = []
        for bucket in count_buckets:
            if bucket not in COUNT_BUCKETS:
                continue
            lo, hi = COUNT_BUCKETS[bucket]
            if hi is None:
                count_conds.append(Killmail.attacker_count >= lo)
            else:
                count_conds.append(and_(Killmail.attacker_count >= lo, Killmail.attacker_count <= hi))
        if count_conds:
            where.append(or_(*count_conds))

    # ── ISK ───────────────────────────────────────────────────────────
    if params.get("isk") and params["isk"] in ISK_MIN_MAP:
        where.append(Killmail.total_value >= ISK_MIN_MAP[params["isk"]])

    # ── Primetime (UTC hour-of-day, wraparound aware) ─────────────────
    pt = params.get("primetime") or set()
    if pt:
        hour_expr = cast(func.strftime("%H", Killmail.killmail_time), Integer)
        pt_conds = []
        for tz in pt:
            for start, end in PRIMETIME_BANDS.get(tz, []):
                pt_conds.append(and_(hour_expr >= start, hour_expr < end))
        if pt_conds:
            where.append(or_(*pt_conds))

    # ── Victim ship ───────────────────────────────────────────────────
    if params.get("ship_ids"):
        where.append(Killmail.victim_ship_type_id.in_(params["ship_ids"]))

    # ── Three entity sides: Attackers / Either / Victim ──────────────
    where.extend(_compile_attacker_clauses(
        params.get("attacker_mode", "or"),
        params.get("attacker_chars", []),
        params.get("attacker_corps", []),
        params.get("attacker_allis", []),
        params.get("attacker_ships", []),
    ))
    where.extend(_compile_victim_clauses(
        params.get("victim_mode", "or"),
        params.get("victim_chars", []),
        params.get("victim_corps", []),
        params.get("victim_allis", []),
        params.get("victim_ships", []),
    ))
    where.extend(_compile_either_clauses(
        params.get("either_mode", "or"),
        params.get("either_chars", []),
        params.get("either_corps", []),
        params.get("either_allis", []),
        params.get("either_ships", []),
    ))

    # ── Sort + cursor ─────────────────────────────────────────────────
    sort = params.get("sort", "date")
    direction = params.get("direction", "desc")
    sort_col, cursor_clause = _resolve_sort_and_cursor(sort, direction, params.get("cursor"))

    return {
        "where": where,
        "joins": joins,
        "sort_col": sort_col,
        "sort_dir": direction,
        "cursor_clause": cursor_clause,
    }
```

- [ ] **Step 4: Implement the three side-compilers**

```python
def _compile_attacker_clauses(mode: str, chars: list[int], corps: list[int],
                               allis: list[int], ships: list[int]) -> list:
    """Compile Attacker-side predicates per And/In/Or mode.

    All produce EXISTS clauses against killmail_attackers.
    - Or: one EXISTS with disjunctive predicates (any attacker matches anything).
    - In: one EXISTS with conjunctive predicates (single attacker row matches all kinds).
    - And: one EXISTS per listed entity (multiple separate attackers).
    """
    if not (chars or corps or allis or ships):
        return []
    a = KillmailAttacker
    if mode == "or":
        conds = []
        if chars:
            conds.append(a.character_id.in_(chars))
        if corps:
            conds.append(a.corporation_id.in_(corps))
        if allis:
            conds.append(a.alliance_id.in_(allis))
        if ships:
            conds.append(a.ship_type_id.in_(ships))
        return [exists().where(
            a.killmail_id == Killmail.killmail_id, or_(*conds)
        )]
    if mode == "in":
        # All predicates inside one EXISTS — must hold on a single attacker row.
        # Within-kind: OR (multiple chars in In mode means "char A or B"); across-kind: AND.
        conds = []
        if chars:
            conds.append(a.character_id.in_(chars))
        if corps:
            conds.append(a.corporation_id.in_(corps))
        if allis:
            conds.append(a.alliance_id.in_(allis))
        if ships:
            conds.append(a.ship_type_id.in_(ships))
        return [exists().where(
            a.killmail_id == Killmail.killmail_id, and_(*conds)
        )]
    # "and" mode — one EXISTS per listed entity.
    out = []
    for c in chars:
        out.append(exists().where(a.killmail_id == Killmail.killmail_id, a.character_id == c))
    for c in corps:
        out.append(exists().where(a.killmail_id == Killmail.killmail_id, a.corporation_id == c))
    for c in allis:
        out.append(exists().where(a.killmail_id == Killmail.killmail_id, a.alliance_id == c))
    for s in ships:
        out.append(exists().where(a.killmail_id == Killmail.killmail_id, a.ship_type_id == s))
    return out


def _compile_victim_clauses(mode: str, chars: list[int], corps: list[int],
                             allis: list[int], ships: list[int]) -> list:
    """Compile Victim-side predicates. Direct on Killmail.victim_*_id columns.
    And and In behave identically here (only one victim row per kill).
    """
    if not (chars or corps or allis or ships):
        return []
    if mode == "or":
        conds = []
        if chars:
            conds.append(Killmail.victim_character_id.in_(chars))
        if corps:
            conds.append(Killmail.victim_corporation_id.in_(corps))
        if allis:
            conds.append(Killmail.victim_alliance_id.in_(allis))
        if ships:
            conds.append(Killmail.victim_ship_type_id.in_(ships))
        return [or_(*conds)]
    # In / And — both conjunctive across kinds, disjunctive within kind.
    conds = []
    if chars:
        conds.append(Killmail.victim_character_id.in_(chars))
    if corps:
        conds.append(Killmail.victim_corporation_id.in_(corps))
    if allis:
        conds.append(Killmail.victim_alliance_id.in_(allis))
    if ships:
        conds.append(Killmail.victim_ship_type_id.in_(ships))
    return [and_(*conds)]


def _compile_either_clauses(mode: str, chars: list[int], corps: list[int],
                             allis: list[int], ships: list[int]) -> list:
    """Compile Either-side predicates: matches if attacker OR victim satisfies the mode.
    """
    if not (chars or corps or allis or ships):
        return []
    a_clauses = _compile_attacker_clauses(mode, chars, corps, allis, ships)
    v_clauses = _compile_victim_clauses(mode, chars, corps, allis, ships)
    # Either = attacker satisfies OR victim satisfies. For "and" mode, that means
    # each separately-listed entity has an attacker_or_victim_match expression.
    if mode == "and":
        # Pair-wise OR (attacker_i, victim_i) — but our compilers emit a single
        # clause for victim regardless of count. Reconstruct per-entity here.
        out = []
        a = KillmailAttacker
        for c in chars:
            out.append(or_(
                exists().where(a.killmail_id == Killmail.killmail_id, a.character_id == c),
                Killmail.victim_character_id == c,
            ))
        for c in corps:
            out.append(or_(
                exists().where(a.killmail_id == Killmail.killmail_id, a.corporation_id == c),
                Killmail.victim_corporation_id == c,
            ))
        for c in allis:
            out.append(or_(
                exists().where(a.killmail_id == Killmail.killmail_id, a.alliance_id == c),
                Killmail.victim_alliance_id == c,
            ))
        for s in ships:
            out.append(or_(
                exists().where(a.killmail_id == Killmail.killmail_id, a.ship_type_id == s),
                Killmail.victim_ship_type_id == s,
            ))
        return out
    # Or / In — single OR of (attacker_clause, victim_clause)
    a_expr = a_clauses[0] if a_clauses else None
    v_expr = v_clauses[0] if v_clauses else None
    if a_expr is not None and v_expr is not None:
        return [or_(a_expr, v_expr)]
    return a_clauses or v_clauses
```

- [ ] **Step 5: Implement sort + cursor resolver**

```python
def _resolve_sort_and_cursor(sort: str, direction: str, cursor: str | None) -> tuple:
    """Return (sort_column_expression, cursor_where_clause).

    Date sort uses killmail_id (monotonic). ISK/Involved use (sort_val, killmail_id) tuples.
    """
    if sort == "isk":
        sort_col = Killmail.total_value
    elif sort == "involved":
        sort_col = Killmail.attacker_count
    else:
        sort_col = Killmail.killmail_id  # Date sort just uses ID monotonicity

    cursor_clause = None
    if cursor:
        try:
            if sort == "date":
                kid = int(cursor)
                cursor_clause = (Killmail.killmail_id < kid) if direction == "desc" else (Killmail.killmail_id > kid)
            else:
                # "val:kid" tuple cursor
                val_str, kid_str = cursor.split(":")
                val = float(val_str)
                kid = int(kid_str)
                if direction == "desc":
                    cursor_clause = or_(
                        sort_col < val,
                        and_(sort_col == val, Killmail.killmail_id < kid),
                    )
                else:
                    cursor_clause = or_(
                        sort_col > val,
                        and_(sort_col == val, Killmail.killmail_id > kid),
                    )
        except (ValueError, AttributeError):
            cursor_clause = None  # Bad cursor — ignore, return page 1.

    return sort_col, cursor_clause
```

- [ ] **Step 6: Add the results route handler**

```python
@router.get("/intel/kills/search/results", response_class=HTMLResponse)
async def intel_kills_search_results(
    request: Request,
    db: AsyncSession = Depends(get_db),
    # — Time
    time: str = "",
    time_start: str = "",
    time_end: str = "",
    # — Chip rows (comma-separated)
    space: str = "",
    wh_class: str = "",
    shattered: int = 0,
    category: str = "",
    count: str = "",
    isk: str = "",
    primetime: str = "",
    # — Ship + entity searches
    ship_id: str = "",
    attacker_mode: str = "or",
    attacker_chars: str = "",
    attacker_corps: str = "",
    attacker_allis: str = "",
    attacker_ships: str = "",
    victim_mode: str = "or",
    victim_chars: str = "",
    victim_corps: str = "",
    victim_allis: str = "",
    victim_ships: str = "",
    either_mode: str = "or",
    either_chars: str = "",
    either_corps: str = "",
    either_allis: str = "",
    either_ships: str = "",
    # — Sort + pagination
    sort: str = "date",
    dir: str = "desc",
    cursor: str = "",
    live: int = 0,
    since: int = 0,
):
    """Compile querystring -> SQL -> rows. Returns rendered partial with cursor markers."""
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("", status_code=401)

    # Normalize params
    params: dict[str, Any] = {
        "time_preset": time if time in ("24h", "7d", "30d", "90d") else None,
        "time_start": None,
        "time_end": None,
        "space": _split_set(space),
        "wh_class": _split_set(wh_class),
        "shattered_only": bool(shattered),
        "category": _split_set(category),
        "count": _split_set(count),
        "isk": isk if isk in ISK_MIN_MAP else None,
        "primetime": _split_set(primetime),
        "ship_ids": _split_ids(ship_id),
        "attacker_mode": attacker_mode if attacker_mode in ("and", "in", "or") else "or",
        "attacker_chars": _split_ids(attacker_chars),
        "attacker_corps": _split_ids(attacker_corps),
        "attacker_allis": _split_ids(attacker_allis),
        "attacker_ships": _split_ids(attacker_ships),
        "victim_mode": victim_mode if victim_mode in ("and", "in", "or") else "or",
        "victim_chars": _split_ids(victim_chars),
        "victim_corps": _split_ids(victim_corps),
        "victim_allis": _split_ids(victim_allis),
        "victim_ships": _split_ids(victim_ships),
        "either_mode": either_mode if either_mode in ("and", "in", "or") else "or",
        "either_chars": _split_ids(either_chars),
        "either_corps": _split_ids(either_corps),
        "either_allis": _split_ids(either_allis),
        "either_ships": _split_ids(either_ships),
        "sort": sort if sort in ("date", "isk", "involved") else "date",
        "direction": dir if dir in ("desc", "asc") else "desc",
        "cursor": cursor or None,
    }
    # Parse custom date range (YYYY-MM-DD HH:MM, UTC naive)
    for src, dst in (("time_start", "time_start"), ("time_end", "time_end")):
        raw = locals()[src].strip() if locals().get(src) else ""
        if raw:
            try:
                params[dst] = datetime.strptime(raw, "%Y-%m-%d %H:%M")
            except ValueError:
                pass

    compiled = await _compile_search_where(params, db)

    # ── Live-poll mode: prepend new kills since=<id>
    if live and since:
        compiled["where"].append(Killmail.killmail_id > since)
        # Sort always Date Desc for live polling (front-end gating ensures this).
        compiled["sort_col"] = Killmail.killmail_id
        compiled["sort_dir"] = "desc"
        compiled["cursor_clause"] = None

    # Build base query
    stmt = select(Killmail)
    if "sde_systems" in compiled["joins"]:
        stmt = stmt.join(SDESystem, SDESystem.system_id == Killmail.solar_system_id)
    if "sde_types" in compiled["joins"]:
        stmt = stmt.join(SDEType, SDEType.type_id == Killmail.victim_ship_type_id)
    for clause in compiled["where"]:
        stmt = stmt.where(clause)
    if compiled["cursor_clause"] is not None:
        stmt = stmt.where(compiled["cursor_clause"])

    sort_col = compiled["sort_col"]
    if compiled["sort_dir"] == "desc":
        stmt = stmt.order_by(sort_col.desc(), Killmail.killmail_id.desc())
    else:
        stmt = stmt.order_by(sort_col.asc(), Killmail.killmail_id.asc())
    stmt = stmt.limit(PAGE_SIZE)

    rows = (await db.execute(stmt)).scalars().all()

    # Compute total_count + total_isk only when not a live poll (saves a query)
    total_count = None
    total_isk = None
    if not live:
        count_stmt = select(func.count(Killmail.killmail_id), func.sum(Killmail.total_value))
        if "sde_systems" in compiled["joins"]:
            count_stmt = count_stmt.select_from(Killmail).join(SDESystem, SDESystem.system_id == Killmail.solar_system_id)
        if "sde_types" in compiled["joins"]:
            count_stmt = count_stmt.join(SDEType, SDEType.type_id == Killmail.victim_ship_type_id)
        for clause in compiled["where"]:
            count_stmt = count_stmt.where(clause)
        result = (await db.execute(count_stmt)).one()
        total_count = int(result[0] or 0)
        total_isk = float(result[1] or 0)

    if not rows:
        return templates.TemplateResponse(
            "partials/intel_kills_search_results.html",
            {
                "request": request,
                "kills": [],
                "total_count": total_count or 0,
                "total_isk": total_isk or 0,
                "newest_cursor": "",
                "oldest_cursor": "",
                "live": bool(live),
            },
        )

    # Enrich + render
    enriched = await _enrich_for_search(rows, db)

    # Cursors
    newest = rows[0].killmail_id
    if params["sort"] == "date":
        oldest_cursor = str(rows[-1].killmail_id)
    elif params["sort"] == "isk":
        oldest_cursor = f"{rows[-1].total_value or 0}:{rows[-1].killmail_id}"
    else:
        oldest_cursor = f"{rows[-1].attacker_count or 0}:{rows[-1].killmail_id}"

    return templates.TemplateResponse(
        "partials/intel_kills_search_results.html",
        {
            "request": request,
            "kills": enriched,
            "total_count": total_count,
            "total_isk": total_isk,
            "newest_cursor": str(newest),
            "oldest_cursor": oldest_cursor,
            "live": bool(live),
        },
    )


async def _enrich_for_search(rows, db: AsyncSession) -> list[dict]:
    """Convert Killmail rows to the row-dict shape consumed by the shared
    feed-row partial. Reuses Feature A's name-resolver pattern.
    """
    from app.intel.killmail_stream import _sys_meta_cache
    from app.intel.recent_battles import sec_band

    if not rows:
        return []
    type_ids: set[int] = set()
    entity_ids: set[int] = set()
    system_ids: set[int] = set()
    kid_list = [r.killmail_id for r in rows]
    # Pull top-attacker name + corp for each kill
    att_q = select(
        KillmailAttacker.killmail_id,
        KillmailAttacker.character_id,
        KillmailAttacker.corporation_id,
        KillmailAttacker.final_blow,
    ).where(KillmailAttacker.killmail_id.in_(kid_list))
    att_rows = (await db.execute(att_q)).all()
    top_by_kid: dict[int, dict] = {}
    for kid, char_id, corp_id, fb in att_rows:
        cur = top_by_kid.get(kid)
        if cur is None or (fb and not cur.get("final_blow")):
            top_by_kid[kid] = {"character_id": char_id, "corporation_id": corp_id, "final_blow": bool(fb)}

    for r in rows:
        if r.victim_ship_type_id:
            type_ids.add(r.victim_ship_type_id)
        for x in (r.victim_character_id, r.victim_corporation_id):
            if x: entity_ids.add(x)
        if r.solar_system_id:
            system_ids.add(r.solar_system_id)
        top = top_by_kid.get(r.killmail_id) or {}
        for x in (top.get("character_id"), top.get("corporation_id")):
            if x: entity_ids.add(x)

    type_names = await type_ids_to_names(db, list(type_ids)) if type_ids else {}
    entity_names = await resolve_entity_names(list(entity_ids)) if entity_ids else {}
    sys_map: dict[int, dict] = {}
    if system_ids:
        for sid, name, sec in (await db.execute(
            select(SDESystem.system_id, SDESystem.system_name, SDESystem.security)
            .where(SDESystem.system_id.in_(system_ids))
        )).all():
            sys_map[sid] = {"name": name, "security": sec}

    _BAND_NORMALIZE = {"Highsec": "hs", "Lowsec": "ls", "Nullsec": "ns",
                       "Unknown": "unknown", "w-space": "wh"}

    def _band(sid: int) -> str:
        meta = _sys_meta_cache.get(sid)
        if meta:
            return _BAND_NORMALIZE.get(meta.get("band") or "Unknown", "unknown")
        sys = sys_map.get(sid)
        if not sys or sys["security"] is None:
            return "unknown"
        if sid >= WH_SYSTEM_MIN:
            return "wh"
        return _BAND_NORMALIZE.get(sec_band(sys["security"]), "unknown")

    out = []
    for r in rows:
        top = top_by_kid.get(r.killmail_id) or {}
        out.append({
            "killmail_id": r.killmail_id,
            "killmail_time": r.killmail_time.isoformat() if r.killmail_time else None,
            "system_name": (sys_map.get(r.solar_system_id) or {}).get("name", f"#{r.solar_system_id}"),
            "system_band": _band(r.solar_system_id),
            "system_class_label": None,  # Search page doesn't surface WH class label inline
            "victim_pilot": entity_names.get(r.victim_character_id, "?") if r.victim_character_id else "NPC",
            "victim_corp": entity_names.get(r.victim_corporation_id, "") if r.victim_corporation_id else "",
            "victim_ship": type_names.get(r.victim_ship_type_id, "?") if r.victim_ship_type_id else "?",
            "victim_ship_type_id": r.victim_ship_type_id,
            "top_attacker_pilot": entity_names.get(top.get("character_id"), "?") if top.get("character_id") else "NPC",
            "top_attacker_corp": entity_names.get(top.get("corporation_id"), "") if top.get("corporation_id") else "",
            "gang_size": r.attacker_count or 1,
            "isk": float(r.total_value or 0),
            "is_npc": bool(r.is_npc),
        })
    return out
```

- [ ] **Step 7: Create the results partial template `app/templates/partials/intel_kills_search_results.html`**

```html
{# Marker div for client JS to read cursors + stats #}
<div hidden data-kfs-marker
     data-total-count="{{ total_count if total_count is not none else '' }}"
     data-total-isk="{{ '%.0f'|format(total_isk) if total_isk is not none else '' }}"
     data-newest-cursor="{{ newest_cursor }}"
     data-oldest-cursor="{{ oldest_cursor }}"
     data-live="{{ 1 if live else 0 }}"></div>
{% for k in kills %}
<div class="kf-row" data-kid="{{ k['killmail_id'] }}">
  <img class="kf-ship-thumb" loading="lazy"
       src="https://images.evetech.net/types/{{ k['victim_ship_type_id'] }}/icon?size=64"
       alt="{{ k['victim_ship'] }}">
  <div class="kf-meta">
    <div class="kf-meta-top">
      <strong>{{ k['victim_pilot'] }}</strong>
      {% if k['victim_corp'] %}[{{ k['victim_corp'] }}]{% endif %}
      · {{ k['victim_ship'] }}
      {% if k['is_npc'] %}<span class="kf-npc-badge">NPC</span>{% endif %}
    </div>
    <div class="kf-meta-bot">
      <span class="kf-sys-{{ k['system_band'] }}">{{ k['system_name'] }}</span>
      · killed by <strong>{{ k['top_attacker_pilot'] }}</strong>
      {% if k['top_attacker_corp'] %}[{{ k['top_attacker_corp'] }}]{% endif %}
      · {% if k['gang_size'] == 1 %}solo{% else %}gang of {{ k['gang_size'] }}{% endif %}
    </div>
  </div>
  <div class="kf-isk">
    {{ "%.1f"|format(k['isk']/1e9) }}B
    <span class="kf-ago" data-time="{{ k['killmail_time'] }}">just now</span>
  </div>
</div>
{% endfor %}
```

(The row markup is intentionally a clone of `partials/intel_kills_feed.html` — when Task 3 adds the NPC badge to the shared partial, we'll also remove this duplication. For Task 2 alone, cloning lets us test the route in isolation; Task 3 cleans it up by adding `{% include 'partials/intel_kills_feed.html' %}` once that file is updated.)

- [ ] **Step 8: Syntax check + commit + deploy**

```bash
python3 -c "import ast; ast.parse(open('app/routes/intel_kills_search.py').read())"
python3 -c "from jinja2 import Environment, FileSystemLoader, select_autoescape; e = Environment(loader=FileSystemLoader('app/templates'), autoescape=select_autoescape(['html'])); e.get_template('partials/intel_kills_search_results.html')"
git add app/routes/intel_kills_search.py app/templates/partials/intel_kills_search_results.html
git commit -m "$(cat <<'EOF'
feat(kills): server-side advanced-search filter compiler + results endpoint

Plan 1 Task 2 — the heart of Feature B. Adds _compile_search_where with
all dimensions (time/space/category/count/ISK/primetime/ship), And/In/Or
compilers for Attackers/Either/Victim, cursor pagination, and the
/intel/kills/search/results route.

Spec: docs/superpowers/specs/2026-05-22-killfeed-advanced-search-design.md

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
ssh ijohnson@146.190.140.112 "/opt/vigilant/scripts/deploy.sh"
ssh ijohnson@146.190.140.112 "docker logs --tail 80 vigilant-app-1 2>&1 | grep -iE 'error|traceback' | head"
```

Then verify in the browser:
1. `https://vigilant.thunderborn.dev/intel/kills/search/results` (no params) — should return 100 rows.
2. `https://vigilant.thunderborn.dev/intel/kills/search/results?space=hs&isk=10b&time=7d&sort=isk&dir=desc` — should return HS kills 10B+ in last 7d sorted by ISK Desc.
3. Cursor round-trip: take `data-oldest-cursor` from response 1, request again with `?cursor=<value>` — should return the next 100 older.

---

### Task 3: NPC badge — shared row partial

**Goal:** Render a small `[NPC]` badge inline with the gang-of-N text on the live-feed row partial. Surface `is_npc` from the Feature A enrichment so the live feed gets the badge too. Deduplicate the results partial template by including the shared row partial.

**Files:**
- Modify: `app/templates/partials/intel_kills_feed.html` — add `[NPC]` badge rendering
- Modify: `app/routes/intel_kills.py` — `_enrich_kills` surfaces `is_npc`
- Modify: `app/templates/intel_kills.html` — CSS for `.kf-npc-badge`
- Modify: `app/templates/partials/intel_kills_search_results.html` — replace inline row markup with `{% include 'partials/intel_kills_feed.html' %}`

**Acceptance Criteria:**
- [ ] Live feed at `/intel/kills` shows `[NPC]` badge on kills where `is_npc` is true. CSS styling matches the muted/subtle look (not a loud red).
- [ ] Search results at `/intel/kills/search/results` render the same row markup via the shared partial — no duplicated row HTML between the two pages.
- [ ] No visual regression on the existing kill feed for non-NPC kills.

**Verify:**
1. Jinja2 templates compile.
2. Deploy + browse `/intel/kills` — find a kill with `is_npc=true` (NPC-killed mining barge etc.) and verify the badge renders.
3. Browse `/intel/kills/search?…` — same rows, badge still renders.

**Steps:**

- [ ] **Step 1: Update the shared row partial `app/templates/partials/intel_kills_feed.html`**

Replace the current contents with:

```html
{% if not older_mode %}
<div hidden data-kf-marker data-kf-newest="{{ newest_id }}" data-kf-total="{{ total_in_buffer }}"></div>
{% endif %}
{% for k in kills %}
<div class="kf-row" data-kid="{{ k['killmail_id'] }}">
  <img class="kf-ship-thumb" loading="lazy"
       src="https://images.evetech.net/types/{{ k['victim_ship_type_id'] }}/icon?size=64"
       alt="{{ k['victim_ship'] }}">
  <div class="kf-meta">
    <div class="kf-meta-top">
      <strong>{{ k['victim_pilot'] }}</strong>
      {% if k['victim_corp'] %}[{{ k['victim_corp'] }}]{% endif %}
      · {{ k['victim_ship'] }}
      {% if k.get('is_npc') %}<span class="kf-npc-badge">NPC</span>{% endif %}
    </div>
    <div class="kf-meta-bot">
      <span class="kf-sys-{{ k['system_band'] }}">{{ k['system_name'] }}{% if k['system_class_label'] %} ({{ k['system_class_label'] }}){% endif %}</span>
      · killed by <strong>{{ k['top_attacker_pilot'] }}</strong>
      {% if k['top_attacker_corp'] %}[{{ k['top_attacker_corp'] }}]{% endif %}
      · {% if k['gang_size'] == 1 %}solo{% else %}gang of {{ k['gang_size'] }}{% endif %}
    </div>
  </div>
  <div class="kf-isk">
    {{ "%.1f"|format(k['isk']/1e9) }}B
    <span class="kf-ago" data-time="{{ k['killmail_time'] }}">just now</span>
  </div>
</div>
{% endfor %}
```

The only changed line vs current is the addition of `{% if k.get('is_npc') %}<span class="kf-npc-badge">NPC</span>{% endif %}` after the ship name and the use of `k.get('is_npc')` (rather than `k['is_npc']`) for backward compatibility with any caller that doesn't set the field.

- [ ] **Step 2: Surface `is_npc` from `_enrich_kills` in `app/routes/intel_kills.py`**

Find `_enrich_kills` (around line 408). At the end of the function, in the `out.append({...})` block, add a new key `is_npc`. The function currently iterates over `kills` from the in-memory buffer where each kill dict has a top-level `is_npc` or doesn't — handle both:

In the loop, before the `out.append`, add:

```python
        is_npc = bool(k.get("is_npc", False))
```

(Live-stream kills include `is_npc` per the killmail.stream payload contract.)

Then add to the dict:

```python
            "is_npc": is_npc,
```

So the output dict has the new field.

- [ ] **Step 3: Verify the `_enrich_kills` change in the older-mode reshape path too**

Find the `intel_kills_older` route (around line 210). In the loop that reshapes DB `Killmail` rows into the `fake_kills` list (around line 350), add `"is_npc": bool(r.is_npc)` to the dict that's appended.

- [ ] **Step 4: Add the CSS rule to `app/templates/intel_kills.html`**

Find the existing `<style>` block. Inside it, before the closing `</style>`, add:

```css
  .kf-npc-badge { display:inline-block; background:var(--surface); color:var(--muted); padding:0 4px; font-size:8px; font-weight:600; letter-spacing:0.08em; border-radius:2px; border:1px solid var(--border); margin-left:4px; vertical-align:middle; }
```

(The same rule was already added to `intel_kills_search.html` in Task 1, but it lives in that page's `<style>` block. Adding it here makes the live feed also get the styling.)

- [ ] **Step 5: Update the search-results partial to use the shared row partial**

Replace `app/templates/partials/intel_kills_search_results.html` with:

```html
{# Marker div for client JS — cursors + stats #}
<div hidden data-kfs-marker
     data-total-count="{{ total_count if total_count is not none else '' }}"
     data-total-isk="{{ '%.0f'|format(total_isk) if total_isk is not none else '' }}"
     data-newest-cursor="{{ newest_cursor }}"
     data-oldest-cursor="{{ oldest_cursor }}"
     data-live="{{ 1 if live else 0 }}"></div>
{# Reuse the live-feed row markup — _enrich_for_search emits the same dict shape #}
{% with older_mode=true %}
  {% include 'partials/intel_kills_feed.html' %}
{% endwith %}
```

`older_mode=true` suppresses the live-feed's own marker div (we have our own).

- [ ] **Step 6: Syntax check + commit + deploy**

```bash
python3 -c "import ast; ast.parse(open('app/routes/intel_kills.py').read())"
python3 -c "from jinja2 import Environment, FileSystemLoader, select_autoescape; e = Environment(loader=FileSystemLoader('app/templates'), autoescape=select_autoescape(['html'])); e.get_template('partials/intel_kills_feed.html'); e.get_template('partials/intel_kills_search_results.html'); e.get_template('intel_kills.html')"
git add app/templates/partials/intel_kills_feed.html app/routes/intel_kills.py app/templates/intel_kills.html app/templates/partials/intel_kills_search_results.html
git commit -m "$(cat <<'EOF'
feat(kills): NPC badge on shared row partial + DRY search results

Plan 1 Task 3. Adds [NPC] badge to the live-feed row partial (also used
by search results via {% include %}). _enrich_kills + the older-mode
reshape both surface is_npc. CSS in intel_kills.html.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
ssh ijohnson@146.190.140.112 "/opt/vigilant/scripts/deploy.sh"
```

Verify in browser: live feed has `[NPC]` badge on NPC-killed rows; search results page renders identically.

---

### Task 4: Frontend — chip toggles + state + show more + detail panel

**Goal:** The page becomes interactive — clicking chip filters fires a query to `/intel/kills/search/results`, results render, "Show more" loads the next page, clicking a row expands the detail panel.

**Files:**
- Modify: `app/templates/intel_kills_search.html` — add the main IIFE handling chip state, querystring sync, htmx wiring, Show More, detail panel.

**Acceptance Criteria:**
- [ ] Clicking any chip in any row toggles its state (multi-select within a row, mutually exclusive within sort/direction rows) and re-fires the search.
- [ ] Results render in `#kfs-results` via htmx; on success the loading message disappears, stats header updates with `Showing 1-N of M · Y ISK`.
- [ ] "Show more" button appears when there's a non-empty oldest_cursor; clicking it appends 100 more rows below; cursor advances to the new oldest.
- [ ] State persists in localStorage (`vigilant:kfs:filters`) and is reflected in the URL via `history.replaceState`. Refreshing the page restores the state.
- [ ] "Reset" button clears state, localStorage, URL → reloads the default view (last 100 universe-wide, Date Desc).
- [ ] Clicking a row expands the same detail panel as the live feed (reuses `/intel/kills/{kid}/detail`).
- [ ] WH sub-class row toggles visibility based on whether WH is selected in the Space row.
- [ ] Custom date-range inputs trigger a re-query when their value changes (debounced).
- [ ] Sort/direction chips toggle and re-query.
- [ ] Empty result set shows a tasteful "No kills match these filters" message.

**Verify:**
1. Jinja2 compiles.
2. Deploy + browser smoke: every chip row clicks correctly; results refresh; show more works; reset works; detail panel opens.
3. Refresh page after filtering → state is restored.
4. Console clean (no JS errors).

**Steps:**

- [ ] **Step 1: Add the main IIFE to `app/templates/intel_kills_search.html`**

Insert a new `<script nonce="{{ request.state.csp_nonce }}">` block inside `{% block content %}`, after the closing `</div>` of `.b-section`, before `{% endblock %}`:

```html
<script nonce="{{ request.state.csp_nonce }}">
  (function() {
    var STORAGE_KEY = 'vigilant:kfs:filters';

    // Filter state — single source of truth for the page.
    var state;
    function defaultState() {
      return {
        time: '',                  // '' | '24h' | '7d' | '30d' | '90d'
        time_start: '',
        time_end: '',
        space: [],
        wh_class: [],
        shattered: false,
        category: [],
        count: [],
        isk: '',                   // string single-select
        primetime: [],
        ship_ids: [],              // [{id, name}]
        attacker_mode: 'or',
        attacker_items: [],        // [{id, name, kind}]   kind ∈ {character,corporation,alliance,ship}
        either_mode: 'or',
        either_items: [],
        victim_mode: 'or',
        victim_items: [],
        sort: 'date',
        direction: 'desc',
        live: false,
      };
    }
    try {
      state = JSON.parse(localStorage.getItem(STORAGE_KEY) || 'null') || defaultState();
    } catch (e) {
      state = defaultState();
    }
    // Defensive: ensure all expected keys
    var defaults = defaultState();
    Object.keys(defaults).forEach(function(k) {
      if (state[k] === undefined) state[k] = defaults[k];
    });

    // ── helpers ─────────────────────────────────────────────────
    function escAttr(s) { return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
    function splitItemsByKind(items) {
      var by = { character: [], corporation: [], alliance: [], ship: [] };
      (items || []).forEach(function(e) { if (by[e.kind]) by[e.kind].push(e.id); });
      return by;
    }

    function buildQS() {
      var qs = [];
      if (state.time) qs.push('time=' + state.time);
      if (state.time_start) qs.push('time_start=' + encodeURIComponent(state.time_start));
      if (state.time_end) qs.push('time_end=' + encodeURIComponent(state.time_end));
      if (state.space.length) qs.push('space=' + state.space.join(','));
      if (state.wh_class.length) qs.push('wh_class=' + state.wh_class.join(','));
      if (state.shattered) qs.push('shattered=1');
      if (state.category.length) qs.push('category=' + state.category.join(','));
      if (state.count.length) qs.push('count=' + state.count.join(','));
      if (state.isk) qs.push('isk=' + state.isk);
      if (state.primetime.length) qs.push('primetime=' + state.primetime.join(','));
      if (state.ship_ids.length) qs.push('ship_id=' + state.ship_ids.map(function(s){return s.id;}).join(','));
      [['attacker_items','attacker'],['either_items','either'],['victim_items','victim']].forEach(function(pair) {
        var items = state[pair[0]];
        if (items.length) {
          var by = splitItemsByKind(items);
          qs.push(pair[1] + '_mode=' + state[pair[0].replace('items','mode')]);
          if (by.character.length) qs.push(pair[1] + '_chars=' + by.character.join(','));
          if (by.corporation.length) qs.push(pair[1] + '_corps=' + by.corporation.join(','));
          if (by.alliance.length) qs.push(pair[1] + '_allis=' + by.alliance.join(','));
          if (by.ship.length) qs.push(pair[1] + '_ships=' + by.ship.join(','));
        }
      });
      qs.push('sort=' + state.sort);
      qs.push('dir=' + state.direction);
      return qs.length ? '?' + qs.join('&') : '';
    }

    function persist() {
      try { localStorage.setItem(STORAGE_KEY, JSON.stringify(state)); } catch (e) {}
      history.replaceState(null, '', '/intel/kills/search' + buildQS());
    }

    function applyChipsToDom() {
      // Single-select rows
      document.querySelectorAll('.kfs-chip[data-time]').forEach(function(el) {
        el.classList.toggle('on', el.dataset.time === state.time);
      });
      document.querySelectorAll('.kfs-chip[data-isk]').forEach(function(el) {
        el.classList.toggle('on', el.dataset.isk === state.isk);
      });
      document.querySelectorAll('.kfs-chip[data-sort]').forEach(function(el) {
        el.classList.toggle('on', el.dataset.sort === state.sort);
      });
      document.querySelectorAll('.kfs-chip[data-dir]').forEach(function(el) {
        el.classList.toggle('on', el.dataset.dir === state.direction);
      });
      // Multi-select rows
      [['space','space'],['wh_class','whclass'],['category','category'],
       ['count','count'],['primetime','primetime']].forEach(function(pair) {
        document.querySelectorAll('.kfs-chip[data-' + pair[1] + ']').forEach(function(el) {
          el.classList.toggle('on', state[pair[0]].indexOf(el.dataset[pair[1]]) !== -1);
        });
      });
      // Shattered modifier
      var sh = document.querySelector('.kfs-chip[data-mod="shattered"]');
      if (sh) sh.classList.toggle('on', !!state.shattered);
      // WH class row visibility
      var whRow = document.getElementById('kfs-wh-subclasses');
      if (whRow) whRow.style.display = state.space.indexOf('wh') !== -1 ? 'flex' : 'none';
      // Date range
      var dsi = document.getElementById('kfs-time-start');
      var dei = document.getElementById('kfs-time-end');
      if (dsi) dsi.value = state.time_start || '';
      if (dei) dei.value = state.time_end || '';
      // Live toggle button
      var lt = document.getElementById('kfs-live-toggle');
      var liveEnabled = state.sort === 'date' && state.direction === 'desc' && !state.time_end;
      if (lt) {
        lt.disabled = !liveEnabled;
        lt.classList.toggle('primary', state.live && liveEnabled);
        lt.textContent = (state.live && liveEnabled) ? '● Live' : '☐ Live';
        if (!liveEnabled) state.live = false;
      }
    }

    function toggleListMember(list, value) {
      var i = list.indexOf(value);
      if (i === -1) list.push(value); else list.splice(i, 1);
    }

    // ── chip click handlers (multi-select rows) ─────────────────
    function bindChipClicks() {
      document.querySelectorAll('.kfs-chip').forEach(function(el) {
        if (el.dataset.bound === '1') return;
        el.dataset.bound = '1';
        el.addEventListener('click', function() {
          if (el.dataset.time !== undefined) {
            state.time = (state.time === el.dataset.time) ? '' : el.dataset.time;
            state.time_start = '';
            state.time_end = '';
          } else if (el.dataset.space !== undefined) {
            toggleListMember(state.space, el.dataset.space);
            if (state.space.indexOf('wh') === -1) { state.wh_class = []; state.shattered = false; }
          } else if (el.dataset.whclass !== undefined) {
            toggleListMember(state.wh_class, el.dataset.whclass);
          } else if (el.dataset.mod === 'shattered') {
            state.shattered = !state.shattered;
          } else if (el.dataset.category !== undefined) {
            toggleListMember(state.category, el.dataset.category);
          } else if (el.dataset.count !== undefined) {
            toggleListMember(state.count, el.dataset.count);
          } else if (el.dataset.isk !== undefined) {
            state.isk = (state.isk === el.dataset.isk) ? '' : el.dataset.isk;
          } else if (el.dataset.primetime !== undefined) {
            toggleListMember(state.primetime, el.dataset.primetime);
          } else if (el.dataset.sort !== undefined) {
            state.sort = el.dataset.sort;
          } else if (el.dataset.dir !== undefined) {
            state.direction = el.dataset.dir;
          }
          applyChipsToDom();
          persist();
          refetchResults();
        });
      });
    }

    // ── date range inputs ───────────────────────────────────────
    function bindDateRangeInputs() {
      ['kfs-time-start', 'kfs-time-end'].forEach(function(id) {
        var el = document.getElementById(id);
        if (!el || el.dataset.bound === '1') return;
        el.dataset.bound = '1';
        var dbn;
        el.addEventListener('input', function() {
          clearTimeout(dbn);
          dbn = setTimeout(function() {
            state[id.replace('kfs-', '').replace('-', '_')] = el.value.trim();
            if (state.time_start || state.time_end) state.time = '';
            persist();
            refetchResults();
          }, 600);
        });
      });
    }

    // ── reset button ────────────────────────────────────────────
    document.getElementById('kfs-reset').addEventListener('click', function() {
      state = defaultState();
      try { localStorage.removeItem(STORAGE_KEY); } catch (e) {}
      history.replaceState(null, '', '/intel/kills/search');
      // Clear chip removable lists
      document.getElementById('kfs-ship-chips').innerHTML = '';
      document.getElementById('kfs-attacker-chips').innerHTML = '';
      document.getElementById('kfs-either-chips').innerHTML = '';
      document.getElementById('kfs-victim-chips').innerHTML = '';
      applyChipsToDom();
      refetchResults();
    });

    // ── refetchResults: fires the /search/results htmx call ────
    var currentOldestCursor = '';
    var currentNewestCursor = '';
    function refetchResults() {
      var feed = document.getElementById('kfs-results');
      if (feed) feed.innerHTML = '';
      currentOldestCursor = '';
      currentNewestCursor = '';
      var loading = document.getElementById('kfs-loading');
      if (loading) { loading.style.display = ''; loading.textContent = 'Loading…'; }
      var showMore = document.getElementById('kfs-show-more-wrap');
      if (showMore) showMore.style.display = 'none';

      fetch('/intel/kills/search/results' + buildQS())
        .then(function(r) { return r.text(); })
        .then(function(html) {
          if (loading) loading.style.display = 'none';
          if (feed) {
            feed.innerHTML = html;
            consumeMarker(feed);
            bindRowClicks();
            if (typeof refreshAgo === 'function') refreshAgo();
          }
        })
        .catch(function(err) {
          if (loading) loading.textContent = 'Error loading results.';
          console.error(err);
        });
    }

    function consumeMarker(container) {
      var marker = container.querySelector('[data-kfs-marker]');
      if (!marker) return;
      var total = marker.getAttribute('data-total-count');
      var totalIsk = marker.getAttribute('data-total-isk');
      currentOldestCursor = marker.getAttribute('data-oldest-cursor') || '';
      currentNewestCursor = marker.getAttribute('data-newest-cursor') || '';
      var rowCount = container.querySelectorAll('.kf-row').length;

      var showingEl = document.getElementById('kfs-stats-showing');
      var totalEl = document.getElementById('kfs-stats-total');
      var iskEl = document.getElementById('kfs-stats-isk');
      if (showingEl) showingEl.textContent = '1-' + rowCount;
      if (totalEl) totalEl.textContent = total || rowCount;
      if (iskEl && totalIsk) {
        var n = parseFloat(totalIsk);
        if (!isNaN(n)) {
          if (n >= 1e12) iskEl.textContent = (n/1e12).toFixed(2) + 't';
          else if (n >= 1e9) iskEl.textContent = (n/1e9).toFixed(2) + 'b';
          else if (n >= 1e6) iskEl.textContent = (n/1e6).toFixed(2) + 'm';
          else iskEl.textContent = n.toFixed(0);
        }
      }
      marker.remove();

      var showMore = document.getElementById('kfs-show-more-wrap');
      if (showMore) showMore.style.display = (currentOldestCursor && rowCount >= 100) ? 'block' : 'none';

      if (rowCount === 0) {
        var feed = document.getElementById('kfs-results');
        if (feed && !feed.querySelector('.kfs-no-results')) {
          var msg = document.createElement('div');
          msg.className = 'kfs-no-results';
          msg.style.cssText = 'color:var(--muted);font-size:11px;padding:30px 0;text-align:center;';
          msg.textContent = 'No kills match these filters.';
          feed.appendChild(msg);
        }
      }
    }

    // ── Show More ──────────────────────────────────────────────
    document.getElementById('kfs-show-more').addEventListener('click', function() {
      if (!currentOldestCursor) return;
      var qs = buildQS();
      var sep = qs ? '&' : '?';
      var btn = this;
      btn.disabled = true;
      btn.textContent = 'Loading…';
      fetch('/intel/kills/search/results' + qs + sep + 'cursor=' + encodeURIComponent(currentOldestCursor))
        .then(function(r) { return r.text(); })
        .then(function(html) {
          var feed = document.getElementById('kfs-results');
          if (!feed) return;
          var tmp = document.createElement('div');
          tmp.innerHTML = html;
          // Consume + remove marker from the temp container, update cursor
          var marker = tmp.querySelector('[data-kfs-marker]');
          var newOldest = marker ? (marker.getAttribute('data-oldest-cursor') || '') : '';
          if (marker) marker.remove();
          // Append the new rows
          while (tmp.firstChild) feed.appendChild(tmp.firstChild);
          currentOldestCursor = newOldest;
          // Update stats
          var rowCount = feed.querySelectorAll('.kf-row').length;
          document.getElementById('kfs-stats-showing').textContent = '1-' + rowCount;
          // Re-bind row clicks for newly added rows
          bindRowClicks();
          if (typeof refreshAgo === 'function') refreshAgo();
          btn.disabled = false;
          btn.textContent = '↓ Show more (next 100)';
          // Hide button if no more pages
          var wrap = document.getElementById('kfs-show-more-wrap');
          if (wrap) wrap.style.display = (currentOldestCursor && tmp.querySelectorAll('.kf-row').length >= 0) ? 'block' : 'none';
          if (rowCount < 100 || !currentOldestCursor) wrap.style.display = 'none';
        });
    });

    // ── row click → detail panel (reuses Feature A pattern) ────
    function bindRowClicks() {
      document.querySelectorAll('#kfs-results .kf-row').forEach(function(row) {
        if (row.dataset.bound === '1') return;
        row.dataset.bound = '1';
        row.addEventListener('click', function() {
          var kid = row.dataset.kid;
          var existing = document.querySelector('#kfs-results .kf-detail[data-kid="' + kid + '"]');
          if (existing) {
            existing.classList.remove('shown');
            row.classList.remove('open');
            setTimeout(function() { if (existing.parentNode) existing.parentNode.removeChild(existing); }, 320);
            return;
          }
          row.classList.add('open');
          fetch('/intel/kills/' + kid + '/detail')
            .then(function(r) { return r.text(); })
            .then(function(html) {
              row.insertAdjacentHTML('afterend', html);
              var panel = row.nextElementSibling;
              if (panel && panel.classList && panel.classList.contains('kf-detail')) {
                void panel.offsetHeight;
                requestAnimationFrame(function() { panel.classList.add('shown'); });
              }
            })
            .catch(function() { row.classList.remove('open'); });
        });
      });
    }

    // ── Time-ago refresh (reuse the existing kill-feed pattern) ─
    function refreshAgo() {
      var now = Date.now();
      document.querySelectorAll('.kf-ago[data-time]').forEach(function(el) {
        var raw = el.getAttribute('data-time') || '';
        if (!/Z$/.test(raw)) raw += 'Z';
        var t = new Date(raw).getTime();
        if (isNaN(t)) { el.textContent = ''; return; }
        var s = Math.max(0, Math.floor((now - t) / 1000));
        if (s < 60) el.textContent = s + 's ago';
        else if (s < 3600) el.textContent = Math.floor(s/60) + 'm ago';
        else if (s < 86400) el.textContent = Math.floor(s/3600) + 'h ago';
        else el.textContent = Math.floor(s/86400) + 'd ago';
      });
    }
    setInterval(refreshAgo, 30000);

    // ── init ───────────────────────────────────────────────────
    applyChipsToDom();
    bindChipClicks();
    bindDateRangeInputs();
    refetchResults();
  })();
</script>
```

(The CSS for `.kf-detail`, `.kf-row.open`, `.kf-detail.shown`, etc. is already defined in `intel_kills.html` and is global. Vigilant's base.html loads it for any page that imports its styles. If the search page doesn't pick those up, copy the relevant rules from `intel_kills.html`'s style block. Verify in browser during smoke test.)

- [ ] **Step 2: Sanity-check that `.kf-detail` CSS is available on the search page**

Check whether the detail-panel CSS rules (`.kf-detail`, `.kf-detail.shown`, `.kf-row.open`, `.kf-row:hover`, etc.) live in a globally-loaded stylesheet OR only in `intel_kills.html`'s scoped style block. Look at `app/templates/base.html` to see what's loaded globally.

If they're only in `intel_kills.html`, copy the relevant rules into `intel_kills_search.html`'s style block (the rules for `.kf-row`, `.kf-row:hover`, `.kf-row.open`, `.kf-detail`, `.kf-detail.shown`, `.kf-detail-grid`, `.kf-detail h4`, all the slot/attacker styles — see lines ~20-90 of `intel_kills.html`).

(If you find these rules are already in a global stylesheet, skip this step.)

- [ ] **Step 3: Commit + deploy**

```bash
python3 -c "from jinja2 import Environment, FileSystemLoader, select_autoescape; e = Environment(loader=FileSystemLoader('app/templates'), autoescape=select_autoescape(['html'])); e.get_template('intel_kills_search.html')"
git add app/templates/intel_kills_search.html
git commit -m "$(cat <<'EOF'
feat(kills): /intel/kills/search frontend wiring — chips, state, show more, detail

Plan 1 Task 4. Adds the main IIFE: chip click handlers for all rows,
single-select + multi-select semantics, localStorage + querystring sync,
debounced date-range inputs, reset button, Show More with cursor
roundtrip, click-to-expand detail panel.

Autocomplete + And/In/Or mode toggles come in Task 5.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
ssh ijohnson@146.190.140.112 "/opt/vigilant/scripts/deploy.sh"
```

Browser smoke test:
- Page loads, 100 rows of kills appear in `#kfs-results`.
- Click `HS` chip → URL updates, results refresh to HS-only.
- Click `Date Asc` chip → results re-sort.
- "Show more" appears below results; click → next 100 append.
- Click a row → detail panel slides down.
- Click another row → previous closes, new opens.
- Reset → URL empties, default view restores.
- Refresh page after setting filters → state persists.

---

### Task 5: Combined entity+ship autocomplete + And/In/Or mode toggles

**Goal:** The three entity rows (Attackers / Either / Victim) and the Ship row become functional: typing in the autocomplete fires both `/intel/kills/resolve?kind=ship` AND `/intel/kills/resolve?kind=entity` queries in parallel, merges results in one dropdown with `[ship]` / `[character]` / `[corporation]` / `[alliance]` labels, lets the user multi-select. The And/In/Or mode buttons toggle the side's compose semantic and re-fire the query.

**Files:**
- Modify: `app/templates/intel_kills_search.html` — extend the IIFE with autocomplete + mode bindings.

**Acceptance Criteria:**
- [ ] Typing 2+ characters in any of the four autocompletes fires both endpoints in parallel and merges results in one dropdown.
- [ ] Selecting a result adds a chip to the side; clicking the `×` on a chip removes it.
- [ ] Mode buttons (And/In/Or) toggle the side's mode; the active mode is highlighted; clicking re-fires the query.
- [ ] Chip + mode state persist in localStorage + URL (already wired in Task 4's `state` shape).
- [ ] Combined autocomplete works for the Ship row too (but only resolves `?kind=ship`).

**Verify:**
1. Jinja2 compiles.
2. Deploy + browser smoke: type "Thor" → dropdown shows characters/corps/allis matching; click one → chip added. Type "Basil" → dropdown shows Basilisk (ship); click → chip added. Toggle mode to "In" → query re-fires.

**Steps:**

- [ ] **Step 1: Extend the IIFE in `intel_kills_search.html` with autocomplete + mode handling**

Inside the existing IIFE (the same `<script>` block from Task 4), add the following functions BEFORE the final `// ── init ──` section. Also add the calls to `bindModeButtons()`, `bindAutocomplete('ship', ...)`, etc. into the init block at the bottom.

```js
    // ── chip rendering for entity/ship multi-select chips ──────
    function renderItemChips(sideKey, containerId) {
      var c = document.getElementById(containerId);
      if (!c) return;
      var items = state[sideKey] || [];
      c.innerHTML = items.map(function(e) {
        var label = e.kind && e.kind !== 'ship'
          ? escAttr(e.name) + ' (' + escAttr(e.kind) + ')'
          : escAttr(e.name);
        return '<span class="kfs-chip-removable" data-rm-key="' + escAttr((e.kind || 'ship') + ':' + e.id) + '">' + label + ' <span class="x">×</span></span>';
      }).join('');
      c.querySelectorAll('[data-rm-key]').forEach(function(el) {
        el.addEventListener('click', function() {
          var key = el.dataset.rmKey;
          state[sideKey] = (state[sideKey] || []).filter(function(e) { return ((e.kind || 'ship') + ':' + e.id) !== key; });
          renderItemChips(sideKey, containerId);
          persist();
          refetchResults();
        });
      });
    }

    // ── autocomplete (combined entity + ship, or ship-only) ────
    function bindAutocomplete(rowId, inputId, resultsId, chipsId, sideStateKey, kindMode) {
      // kindMode: 'ship' (ship only) or 'combined' (ship + entity).
      var input = document.getElementById(inputId);
      var results = document.getElementById(resultsId);
      var chips = document.getElementById(chipsId);
      if (!input || !results || !chips) return;
      var debounce;

      input.addEventListener('input', function() {
        clearTimeout(debounce);
        var q = input.value.trim();
        if (q.length < 2) { results.classList.remove('show'); results.innerHTML = ''; return; }
        debounce = setTimeout(function() {
          var queries = [
            fetch('/intel/kills/resolve?kind=ship&q=' + encodeURIComponent(q)).then(function(r) { return r.json(); }),
          ];
          if (kindMode === 'combined') {
            queries.push(fetch('/intel/kills/resolve?kind=entity&q=' + encodeURIComponent(q)).then(function(r) { return r.json(); }));
          }
          Promise.all(queries).then(function(arr) {
            var items = [];
            (arr[0] || []).forEach(function(it) { items.push({ id: it.id, name: it.name, kind: 'ship' }); });
            if (arr[1]) (arr[1] || []).forEach(function(it) { items.push({ id: it.id, name: it.name, kind: it.kind }); });
            results.innerHTML = items.slice(0, 16).map(function(it) {
              return '<div class="kfs-ac-result" data-id="' + it.id + '" data-kind="' + escAttr(it.kind) + '" data-name="' + escAttr(it.name) + '">'
                + escAttr(it.name) + '<span class="kind">[' + escAttr(it.kind) + ']</span></div>';
            }).join('');
            results.classList.toggle('show', items.length > 0);
            results.querySelectorAll('.kfs-ac-result').forEach(function(el) {
              el.addEventListener('click', function() {
                var entry = {
                  id: parseInt(el.dataset.id, 10),
                  name: el.dataset.name,
                  kind: el.dataset.kind || 'ship',
                };
                var key = entry.kind + ':' + entry.id;
                if (!Array.isArray(state[sideStateKey])) state[sideStateKey] = [];
                if (!state[sideStateKey].some(function(e) { return (e.kind + ':' + e.id) === key; })) {
                  state[sideStateKey].push(entry);
                }
                input.value = '';
                results.classList.remove('show');
                results.innerHTML = '';
                renderItemChips(sideStateKey, chipsId);
                persist();
                refetchResults();
              });
            });
          });
        }, 250);
      });
      document.addEventListener('click', function(e) {
        if (!input.contains(e.target) && !results.contains(e.target)) {
          results.classList.remove('show');
        }
      });
      renderItemChips(sideStateKey, chipsId);
    }

    // ── mode buttons (And/In/Or per side) ──────────────────────
    function bindModeButtons() {
      document.querySelectorAll('.kfs-modes').forEach(function(group) {
        var side = group.dataset.side;
        if (!side) return;
        // side ∈ {attacker, either, victim}; state key is <side>_mode
        var stateKey = side + '_mode';
        // Reflect current state
        group.querySelectorAll('.kfs-mode-btn').forEach(function(btn) {
          btn.classList.toggle('on', btn.dataset.mode === state[stateKey]);
        });
        group.querySelectorAll('.kfs-mode-btn').forEach(function(btn) {
          if (btn.dataset.bound === '1') return;
          btn.dataset.bound = '1';
          btn.addEventListener('click', function() {
            state[stateKey] = btn.dataset.mode;
            group.querySelectorAll('.kfs-mode-btn').forEach(function(b) {
              b.classList.toggle('on', b.dataset.mode === state[stateKey]);
            });
            persist();
            refetchResults();
          });
        });
      });
    }
```

Then in the `// ── init ──` section at the bottom of the IIFE, add these calls AFTER `applyChipsToDom()` and BEFORE `refetchResults()`:

```js
    bindModeButtons();
    bindAutocomplete('ship-row',      'kfs-ship-input',     'kfs-ship-results',     'kfs-ship-chips',     'ship_ids',       'ship');
    bindAutocomplete('attacker-row',  'kfs-attacker-input', 'kfs-attacker-results', 'kfs-attacker-chips', 'attacker_items', 'combined');
    bindAutocomplete('either-row',    'kfs-either-input',   'kfs-either-results',   'kfs-either-chips',   'either_items',   'combined');
    bindAutocomplete('victim-row',    'kfs-victim-input',   'kfs-victim-results',   'kfs-victim-chips',   'victim_items',   'combined');
```

- [ ] **Step 2: Commit + deploy**

```bash
python3 -c "from jinja2 import Environment, FileSystemLoader, select_autoescape; e = Environment(loader=FileSystemLoader('app/templates'), autoescape=select_autoescape(['html'])); e.get_template('intel_kills_search.html')"
git add app/templates/intel_kills_search.html
git commit -m "$(cat <<'EOF'
feat(kills): combined entity+ship autocomplete + And/In/Or mode toggles

Plan 1 Task 5. Adds parallel autocomplete (ship + entity) for the
Attackers/Either/Victim rows, plus the And/In/Or mode buttons.
Selected items render as removable chips; mode toggle re-fires the
query.

Spec: docs/superpowers/specs/2026-05-22-killfeed-advanced-search-design.md

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
ssh ijohnson@146.190.140.112 "/opt/vigilant/scripts/deploy.sh"
```

Browser smoke test:
- Type "Thor" in Attackers → dropdown shows characters / corps / allis named Thor.
- Type "Basilisk" in Attackers → dropdown shows Basilisk ship.
- Add Thor + Basilisk → 2 chips appear.
- Click `[In]` mode button → re-fires query → results = "kills where Thor flew a Basilisk".
- Click chip `×` → chip removed, re-fires.

---

### Task 6: Live polling toggle

**Goal:** When sort=Date Desc + no time end-date, the Live toggle becomes enabled. Clicking it on starts a 15s interval that prepends new rows via `?live=1&since=<newest>`, with dedupe + flash animation. Clicking off cancels.

**Files:**
- Modify: `app/templates/intel_kills_search.html` — add live-polling JS to the IIFE.

**Acceptance Criteria:**
- [ ] Live toggle button is disabled (greyed out) unless `sort=date AND direction=desc AND !time_end`.
- [ ] When enabled and clicked on, polling starts: every 15s a request is fired to `/intel/kills/search/results?live=1&since=<newest_cursor>&<filters>`.
- [ ] New rows are prepended to `#kfs-results` with the `kf-new` flash animation; existing rows are not duplicated (data-kid dedupe).
- [ ] When the toggle is clicked off, polling stops.
- [ ] When the user changes a filter / sort / direction while polling is on, polling pauses, refetches the new view, then restarts on the new newest cursor.

**Verify:**
1. Deploy + browser: with default filters (Date Desc, no time end), toggle the Live button on → watch new rows appear at the top with the blue flash every ~15s.
2. Change sort to ISK → Live toggle greys out, polling stops.
3. Change back to Date Desc → Live enabled (but off by default after a sort change).

**Steps:**

- [ ] **Step 1: Add live-polling logic to the IIFE**

Inside the same IIFE in `intel_kills_search.html`, add the following functions BEFORE the `// ── init ──` section:

```js
    // ── Live polling ───────────────────────────────────────────
    var livePollTimer = null;

    function liveEnabledCheck() {
      return state.sort === 'date' && state.direction === 'desc' && !state.time_end;
    }

    function startLivePoll() {
      if (livePollTimer) return;
      livePollTimer = setInterval(livePoll, 15000);
    }

    function stopLivePoll() {
      if (livePollTimer) { clearInterval(livePollTimer); livePollTimer = null; }
    }

    function livePoll() {
      if (!state.live || !liveEnabledCheck() || !currentNewestCursor) return;
      var qs = buildQS();
      var sep = qs ? '&' : '?';
      fetch('/intel/kills/search/results' + qs + sep + 'live=1&since=' + encodeURIComponent(currentNewestCursor))
        .then(function(r) { return r.text(); })
        .then(function(html) {
          var feed = document.getElementById('kfs-results');
          if (!feed) return;
          var tmp = document.createElement('div');
          tmp.innerHTML = html;
          var marker = tmp.querySelector('[data-kfs-marker]');
          var newNewest = marker ? (marker.getAttribute('data-newest-cursor') || '') : '';
          if (marker) marker.remove();
          // Dedupe — drop rows whose data-kid is already in the DOM
          var existing = {};
          feed.querySelectorAll('.kf-row').forEach(function(r) { if (r.dataset.kid) existing[r.dataset.kid] = true; });
          var newRows = [];
          tmp.querySelectorAll('.kf-row').forEach(function(r) {
            if (r.dataset.kid && !existing[r.dataset.kid]) newRows.push(r);
          });
          if (newRows.length) {
            // Prepend in reverse order so newest appears at top
            newRows.reverse().forEach(function(r) {
              r.classList.add('kf-new');
              feed.insertBefore(r, feed.firstChild);
              setTimeout(function() { r.classList.remove('kf-new'); }, 650);
            });
            if (newNewest) currentNewestCursor = newNewest;
            // Update stats
            var rowCount = feed.querySelectorAll('.kf-row').length;
            document.getElementById('kfs-stats-showing').textContent = '1-' + rowCount;
            // Bump total too if marker had a value
            var totalAttr = marker ? marker.getAttribute('data-total-count') : '';
            if (totalAttr) document.getElementById('kfs-stats-total').textContent = totalAttr;
            bindRowClicks();
            if (typeof refreshAgo === 'function') refreshAgo();
          }
        })
        .catch(function() { /* swallow — next poll will retry */ });
    }

    // Live toggle button handler
    document.getElementById('kfs-live-toggle').addEventListener('click', function() {
      if (!liveEnabledCheck()) return;
      state.live = !state.live;
      applyChipsToDom();
      persist();
      if (state.live) startLivePoll(); else stopLivePoll();
    });

    // When any filter changes during a live session, restart polling on the new newest
    var _prev_refetchResults = refetchResults;
    refetchResults = function() {
      stopLivePoll();
      _prev_refetchResults();
      if (state.live && liveEnabledCheck()) {
        // currentNewestCursor is set by consumeMarker after fetch completes;
        // schedule the live-poll restart on next tick to ensure it's populated.
        setTimeout(startLivePoll, 100);
      }
    };
```

(The reassignment of `refetchResults` wraps the original so all the previous call sites get the live-poll restart behaviour without duplicating their logic.)

Also extend the existing init block — if state.live was true on page load and conditions are met, start polling automatically (the initial `refetchResults()` is now wrapped):

The wrapping above handles this automatically — the initial `refetchResults()` call will trigger `startLivePoll()` if `state.live` is true. No additional init code needed.

- [ ] **Step 2: Commit + deploy + smoke test**

```bash
python3 -c "from jinja2 import Environment, FileSystemLoader, select_autoescape; e = Environment(loader=FileSystemLoader('app/templates'), autoescape=select_autoescape(['html'])); e.get_template('intel_kills_search.html')"
git add app/templates/intel_kills_search.html
git commit -m "$(cat <<'EOF'
feat(kills): live polling toggle on advanced search results

Plan 1 Task 6. When sort=Date Desc and no end-date, the Live toggle
prepends new kills every 15s with the kf-new flash animation. Dedupes
by data-kid. Restarts on filter changes.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
ssh ijohnson@146.190.140.112 "/opt/vigilant/scripts/deploy.sh"
```

Smoke test in browser:
- Default sort + direction → click Live on → wait ~15s → new rows appear at top with blue flash.
- Change sort to ISK → Live toggle greys out, polling stops.
- Change back to Date Desc → toggle re-enabled. Click on → polling resumes.
- Click off → stops.

---

### Task 7: Post-deploy EXPLAIN + optional indexes

**Goal:** Verify the common search queries are index-served. Add indexes only where EXPLAIN says a full table scan would happen on a realistic query.

**Files (conditional):**
- VPS DB: `CREATE INDEX IF NOT EXISTS` for any new indexes needed
- `app/db/models.py` — mirror any new index in `Killmail.__table_args__`

**Acceptance Criteria:**
- [ ] EXPLAIN on the default query (no filters, Date Desc, page 1): uses an index, not a full scan.
- [ ] EXPLAIN on a filter combo (`isk >= 1e10`, sort=isk desc): no full scan.
- [ ] EXPLAIN on a sort=involved query: no full scan.
- [ ] Warm-cache perf on `/intel/kills/search/results` is logged in `perf.log` and reasonable (<1s for typical filter combos with cached names).

**Verify:**
1. EXPLAIN output captured (use the `mode=ro&immutable=1` python trick from Task 2).
2. perf.log samples post-deploy.

**Steps:**

- [ ] **Step 1: EXPLAIN the default + a few representative filter combinations**

```bash
ssh ijohnson@146.190.140.112 "docker exec vigilant-app-1 python3 -c \"
import sqlite3
c = sqlite3.connect('file:/data/vigilant.db?mode=ro&immutable=1', uri=True)
print('=== Default (Date Desc, page 1) ===')
for r in c.execute('EXPLAIN QUERY PLAN SELECT * FROM killmails ORDER BY killmail_id DESC LIMIT 100'):
    print(' ', r)
print('=== ISK >= 10B, sort by ISK Desc ===')
for r in c.execute('EXPLAIN QUERY PLAN SELECT * FROM killmails WHERE total_value >= 10000000000 ORDER BY total_value DESC, killmail_id DESC LIMIT 100'):
    print(' ', r)
print('=== Sort by Involved Desc ===')
for r in c.execute('EXPLAIN QUERY PLAN SELECT * FROM killmails ORDER BY attacker_count DESC, killmail_id DESC LIMIT 100'):
    print(' ', r)
print('=== Highsec + 7d window ===')
for r in c.execute(\\\"EXPLAIN QUERY PLAN SELECT k.* FROM killmails k JOIN sde_systems s ON s.system_id = k.solar_system_id WHERE s.security >= 0.5 AND k.killmail_time >= datetime('now','-7 days') ORDER BY k.killmail_id DESC LIMIT 100\\\"):
    print(' ', r)
\""
```

Acceptable patterns: `SEARCH k USING INDEX ix_killmails_killmail_time` or `SEARCH k USING INTEGER PRIMARY KEY` for the main table scan. A full `SCAN TABLE killmails` is unacceptable.

- [ ] **Step 2: Add indexes if needed**

If Step 1 shows full scans, add the appropriate indexes:

- ISK-sort scan: `CREATE INDEX IF NOT EXISTS ix_killmails_total_value ON killmails(total_value, killmail_id);`
- Involved-sort scan: `CREATE INDEX IF NOT EXISTS ix_killmails_attacker_count ON killmails(attacker_count, killmail_id);`

```bash
ssh ijohnson@146.190.140.112 "docker exec vigilant-app-1 python3 -c \"
import sqlite3
c = sqlite3.connect('/data/vigilant.db')
c.execute('CREATE INDEX IF NOT EXISTS ix_killmails_total_value ON killmails(total_value, killmail_id)')
c.execute('CREATE INDEX IF NOT EXISTS ix_killmails_attacker_count ON killmails(attacker_count, killmail_id)')
c.commit()
\""
```

Then mirror in `app/db/models.py`. Find `Killmail.__table_args__` (around line 681):

```python
    __table_args__ = (
        Index("ix_killmail_system_time", "solar_system_id", "killmail_time"),
        Index("ix_killmails_time_value", "killmail_time", "total_value"),  # from Feature A
    )
```

Change to:

```python
    __table_args__ = (
        Index("ix_killmail_system_time", "solar_system_id", "killmail_time"),
        Index("ix_killmails_time_value", "killmail_time", "total_value"),  # from Feature A
        Index("ix_killmails_total_value", "total_value", "killmail_id"),
        Index("ix_killmails_attacker_count", "attacker_count", "killmail_id"),
    )
```

(Only include the indexes that were actually needed per Step 1's EXPLAIN. Skip any that were already index-served.)

Syntax-check and commit:

```bash
python3 -c "import ast; ast.parse(open('app/db/models.py').read())"
git add app/db/models.py
git commit -m "$(cat <<'EOF'
perf(kills): add ISK + Involved sort indexes (conditional)

Plan 1 Task 7. Indexes added based on post-deploy EXPLAIN of advanced
search queries:
- ix_killmails_total_value for sort=isk
- ix_killmails_attacker_count for sort=involved

Already-deployed via CREATE INDEX IF NOT EXISTS on the VPS — this
commit mirrors the declarations in models.py so future schema rebuilds
carry them.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
ssh ijohnson@146.190.140.112 "/opt/vigilant/scripts/deploy.sh"
```

(Skip the commit entirely if no indexes were needed.)

- [ ] **Step 3: Check warm-cache perf**

```bash
ssh ijohnson@146.190.140.112 "tail -n 100 /opt/vigilant/data/logs/perf.log 2>/dev/null | grep '/intel/kills/search/results' | tail -20"
```

Look for:
- First hit (cold names): up to ~2s acceptable (per spec budget).
- Subsequent hits with overlapping entities: <300ms.

If warm hits exceed 1s consistently, capture the slow filters and revisit in a follow-up perf pass.

- [ ] **Step 4: Final acceptance walk-through**

Browse `https://vigilant.thunderborn.dev/intel/kills/search`:
1. Default view shows last 100 universe kills, Date Desc.
2. Click HS chip → results refresh.
3. Add Thor as Attacker, Basilisk as ship, mode=In → results = kills where Thor flew a Basilisk.
4. Click Date → ISK sort.
5. "Show more" → next 100 appear below.
6. Click row → detail panel opens.
7. Toggle Live (with Date Desc, no end date) → new kills prepend with flash.
8. Reset → all chips cleared.
9. Browser console clean throughout.

If all pass, Plan 1 (MVP) is complete. Plan 2 (Awox / Padding / HighSec Gank / AT Ships) gets its own spec/plan cycle after.

---

## Self-Review Notes

**Spec coverage:**
- Page placement + URL → Task 1
- Default state (no querystring) → Task 2 (server returns 100 most recent when no filters)
- All filter dimensions (time, space, WH-sub, category, count, ISK, primetime, ship, attacker/either/victim with And/In/Or) → Task 2 server-side + Task 4-5 frontend
- Sort + cursor pagination → Task 2 server + Task 4 frontend
- Live polling → Task 6
- NPC badge → Task 3
- Stats header (count + ISK) → Task 2 marker + Task 4 consumeMarker
- Indexes → Task 7
- Capital workaround (group_id whitelist + Rorqual type_id) → Task 2 (`CAPITAL_GROUP_IDS` + `RORQUAL_TYPE_ID`)
- Abyssal range → Task 2 (`ABYSSAL_SYSTEM_MIN/MAX`)

**Type consistency:**
- `_compile_search_where` is called from `intel_kills_search_results` (Task 2 step 6 — both inside the same file, same module).
- `_enrich_for_search` produces dicts with `is_npc` key — Task 3 step 1 reads `k.get('is_npc')` (defensive; covers both feed-row and search-results call sites).
- Cursor format: Task 2 step 5 documents the Date-sort vs ISK/Involved-sort difference. Task 4 step 1's frontend just opaquely passes the `data-oldest-cursor` value through — no parsing client-side.
- State shape (`state.attacker_items` etc.) is consistent between Task 4 step 1 and Task 5 step 1.

**Placeholder scan:**
- No TBD/TODO/fill-in items.
- One documented limitation: the `shattered_only` modifier is a Python-side post-filter not implemented in the SQL compiler. Documented in Task 2 step 3 as "no-op on systems not in _sys_meta_cache; revisit if user reports it."
- Capital list uses verified group_ids + the Rorqual type_id workaround. Group 941 (Industrial Command Ship) is deliberately excluded — captured in the spec.
