# Site Restyle Rollout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Roll the expressive-brutalist design system onto the live Jinja2 site: stylesheet swap in base.html, ambient-flythrough login page fed by real kills, big-bang deploy, full 111-template sweep.

**Architecture:** The design-system stylesheets (`design-system/css/`) become the site's shared vocabulary via a new `/static/ds` mount; `base.html` keeps only site-specific CSS, extracted to `static/css/site.css`. The login page (`index.html`) mounts `vigilant-ambient.js` with a new public `/api/ambient/kills` poll endpoint. Deploy is big-bang with `deploy.sh`'s `:prev` rollback.

**Tech Stack:** FastAPI + Jinja2 + htmx (existing), design-system CSS + ambient ES module (sub-project 1), pytest + pytest-asyncio + aiosqlite (new, dev-only).

**Spec:** `docs/superpowers/specs/2026-07-03-site-restyle-rollout-design.md`

---

## File structure

```
app/main.py                      # Task 1: /static/ds mount + ambient router registration
app/routes/ambient.py            # Task 1: NEW — /api/ambient/kills
tests/test_ambient_kills.py      # Task 1: NEW — query-window tests (in-memory sqlite)
requirements-dev.txt             # Task 1: NEW — pytest, pytest-asyncio (dev only, not in the image)
static/css/site.css              # Task 2: NEW — site-specific CSS extracted from base.html
app/templates/base.html          # Task 2: inline style block → 4 <link> tags + keep scripts
app/templates/index.html         # Task 3: rebuilt login page with ambient
docs/superpowers/plans/sweep-checklist-2026-07-03.md  # Task 5: sweep tracking (created during execution)
```

**base.html style-block disposition map** (current line numbers, before edits):

| Lines | Section | Fate |
|---|---|---|
| 13 (`<style>` open) | — | replaced by `<link>` tags |
| 14–27 tokens | covered by `tokens.css` | DELETE |
| 28–51 reset + body | covered by `components.css` | DELETE |
| 52–464 nav, dropdown, breadcrumbs, tabs, footer, main, page header, section, labels, stats, cards, rows, actions/btn, table rows, progress, dot, badge | covered by `components.css` | DELETE |
| 481–544 banner, server bar, grids, panel, empty | covered by `components.css` | DELETE |
| 465–480 inline badge (contiguous with above) | covered | DELETE |
| 545–554 `.corp-logo` | site-specific | → site.css |
| 555–561 spin/pulse keyframes | covered by `motion.css` | DELETE |
| 562–566 htmx indicator | site-specific | → site.css |
| 567–571 scrollbar | covered by `components.css` | DELETE |
| 572–578 `#esi-banner` override | site-specific | → site.css |
| 579–617 structure alert banners | site-specific | → site.css |
| 618–624 standalone color utilities | covered by `components.css` | DELETE |
| 625–647 focus-visibility rules | site-specific (site-wide `:focus-visible` policy) | → site.css |
| 648–698 hamburger + mobile menu | site-specific | → site.css |
| 699–763 responsive `@media` blocks | site-specific | → site.css |
| 764–770 reduced-motion spin/pulse hold | covered by `motion.css` kill switch | DELETE |
| 771–802 `.b-hover-*` utilities | site-specific | → site.css |
| 803–807 `b-muted`/`b-text`/`b-pad-md`/`b-muted-sm`/`b-eyebrow` | covered by `components.css` (`b-eyebrow` goes gold — intentional restyle) | DELETE |

---

### Task 1: Public kill-events endpoint + /static/ds mount

**Goal:** `GET /api/ambient/kills` returns recent-kill system IDs (public, cached, index-backed) and the design-system directory is served at `/static/ds`.

> **Post-review amendments (after 35db932):** Dockerfile now COPYs design-system/{css,ambient} (the mount otherwise crashes the container — the image is built from selective COPYs, not the whole repo); .dockerignore excludes the react build trees; the kills query is restructured (time-ordered inner select, distinct outer) because SQLite's planner chose a full scan of ix_killmail_system_time on the 192 GB DB; a 15s in-process cache guards the public endpoint; tests/conftest.py provides dummy env so pytest runs clean. Code is authoritative over the Step blocks.

**Files:**
- Create: `app/routes/ambient.py`
- Create: `tests/test_ambient_kills.py`
- Create: `requirements-dev.txt`
- Modify: `app/main.py` (one mount line near line 98; one `include_router` near line 108)

**Acceptance Criteria:**
- [ ] `GET /api/ambient/kills` returns `[{"system_id": <int>}, …]` for kills in the last 120s, max 50, distinct systems, `Cache-Control: public, max-age=15`
- [ ] Endpoint has NO auth dependency and does no writes; any DB error returns `[]` with 200
- [ ] `/static/ds/css/tokens.css` and `/static/ds/ambient/vigilant-ambient.js` are served (verify locally with the mount pointing at `design-system/`)
- [ ] `pytest tests/ -v` green (3 tests)

**Verify:** `python3 -m pytest tests/test_ambient_kills.py -v` → 3 passed; `python3 -c "import ast; ast.parse(open('app/routes/ambient.py').read()); ast.parse(open('app/main.py').read())"` → exit 0

**Steps:**

- [ ] **Step 1: Write the failing test** — `tests/test_ambient_kills.py` (also create empty `tests/__init__.py`):

```python
"""Tests the recent-kill query window in isolation (in-memory SQLite).

The app has import-time side effects (DB/SDE init), so we test the
extracted query function, not the FastAPI route object.
"""
import asyncio
from datetime import datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.db.models import Base, Killmail
from app.routes.ambient import _recent_kill_systems


@pytest.fixture()
def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.get_event_loop().run_until_complete(_init())
    return async_sessionmaker(engine, expire_on_commit=False)


def _km(system_id: int, age_s: int) -> Killmail:
    return Killmail(
        killmail_id=hash((system_id, age_s)) % 10**9,
        killmail_time=datetime.utcnow() - timedelta(seconds=age_s),
        solar_system_id=system_id,
    )


def test_empty_table_returns_empty(session_factory):
    async def run():
        async with session_factory() as s:
            return await _recent_kill_systems(s, window_s=120)
    assert asyncio.get_event_loop().run_until_complete(run()) == []


def test_recent_kill_included_stale_excluded(session_factory):
    async def run():
        async with session_factory() as s:
            s.add(_km(30000142, age_s=30))    # Jita, fresh
            s.add(_km(30002187, age_s=600))   # Amarr, stale
            await s.commit()
            return await _recent_kill_systems(s, window_s=120)
    result = asyncio.get_event_loop().run_until_complete(run())
    assert result == [30000142]


def test_distinct_systems(session_factory):
    async def run():
        async with session_factory() as s:
            s.add(_km(30000142, age_s=10))
            s.add(_km(30000142, age_s=20))
            await s.commit()
            return await _recent_kill_systems(s, window_s=120)
    result = asyncio.get_event_loop().run_until_complete(run())
    assert result == [30000142]
```

NOTE: the `Killmail` model has more nullable columns; only `killmail_id`, `killmail_time`, `solar_system_id` are needed. If `Base.metadata.create_all` trips on unrelated models' SQLite-incompatible DDL, create only the needed table: `await conn.run_sync(lambda c: Killmail.__table__.create(c))`.

- [ ] **Step 2: Create `requirements-dev.txt`** (dev-only; NOT added to the Docker image):

```
pytest>=8
pytest-asyncio>=0.24
aiosqlite
```

Install locally: `python3 -m pip install -r requirements-dev.txt -r requirements.txt` (or into the venv the repo uses; if no venv exists, create one: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt` and run pytest via `.venv/bin/pytest`). Add `.venv/` to `.gitignore` if missing.

- [ ] **Step 3: Run test to verify it fails**

Run: `python3 -m pytest tests/test_ambient_kills.py -v`
Expected: FAIL — `ModuleNotFoundError: app.routes.ambient` (or ImportError on `_recent_kill_systems`).

- [ ] **Step 4: Write `app/routes/ambient.py`**

```python
"""Public ambient-background data: recent kill locations for login-page blips.

Intentionally unauthenticated: returns only solar system IDs of recent kills
(public data — the same kills are on zKillboard). No names, values, or IDs
beyond the system. Read-only.
"""
import logging
from datetime import datetime, timedelta

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import select

from app.db.database import AsyncSessionLocal
from app.db.models import Killmail

logger = logging.getLogger(__name__)
router = APIRouter()

_WINDOW_S = 120
_LIMIT = 50


async def _recent_kill_systems(session, window_s: int = _WINDOW_S) -> list[int]:
    cutoff = datetime.utcnow() - timedelta(seconds=window_s)
    rows = await session.execute(
        select(Killmail.solar_system_id)
        .where(Killmail.killmail_time >= cutoff)
        .group_by(Killmail.solar_system_id)
        .limit(_LIMIT)
    )
    return [r[0] for r in rows.all()]


@router.get("/api/ambient/kills")
async def ambient_kills():
    try:
        async with AsyncSessionLocal() as session:
            systems = await _recent_kill_systems(session)
    except Exception:  # decoration must never 500 the login path
        logger.exception("ambient kills query failed")
        systems = []
    return JSONResponse(
        [{"system_id": s} for s in systems],
        headers={"Cache-Control": "public, max-age=15"},
    )
```

NOTE: confirm the sessionmaker import path — grep `AsyncSessionLocal` in `app/db/`; if it lives elsewhere (e.g. `app.db.session`), adjust the import to match the codebase's convention used by other routers.

- [ ] **Step 5: Wire into `app/main.py`**

Near the existing static mount (line ~98), add AFTER it:

```python
app.mount("/static/ds", StaticFiles(directory="design-system"), name="static_ds")
```

Near the router includes (line ~108), following the existing pattern:

```python
from app.routes.ambient import router as ambient_router
app.include_router(ambient_router)
```

(Match the file's import style — if routers are imported at the top of main.py, put the import there.)

- [ ] **Step 6: Run tests to verify green**

Run: `python3 -m pytest tests/test_ambient_kills.py -v`
Expected: 3 passed.

- [ ] **Step 7: Syntax checks + commit**

```bash
python3 -c "import ast; ast.parse(open('app/routes/ambient.py').read()); ast.parse(open('app/main.py').read())"
git add app/routes/ambient.py app/main.py tests/ requirements-dev.txt .gitignore
git commit -m "feat(ambient): public recent-kills endpoint + /static/ds mount for design-system assets"
```

---

### Task 2: Extract site.css and swap base.html to design-system stylesheets

**Goal:** `base.html`'s 795-line inline style block becomes four `<link>` tags; site-specific CSS lives in `static/css/site.css`.

> **Post-review amendments (after 682e4a1):** stylesheet links carry ?v={{ css_v }} (startup content hash) — the edge serves /static/ as immutable/7d, so unversioned CSS would go stale on every later restyle deploy; the plan's "grep -c b-nav → 0" verify line was wrong (site.css legitimately re-touches .b-nav inside @media overrides — 2 hits is correct); ROLLBACK CAVEAT: never roll back to an image that has the new base.html without the /static/ds mount + Dockerfile COPY (whole site renders unstyled) — Task 1 and 2 commits must deploy together, which deploy.sh's whole-image model guarantees.

**Files:**
- Create: `static/css/site.css`
- Modify: `app/templates/base.html` (style block lines 13–807; notif-dropdown inline z-index ~line 870)

**Acceptance Criteria:**
- [ ] `base.html` `<head>` links, in order: `/static/css/tailwind.css` (existing), `/static/ds/css/tokens.css`, `/static/ds/css/motion.css`, `/static/ds/css/components.css`, `/static/css/site.css`
- [ ] `site.css` contains EXACTLY the "→ site.css" sections from the disposition map, moved verbatim (with the two edits below); nothing covered by the design system is duplicated
- [ ] notif-dropdown inline style uses `z-index:var(--z-dropdown)` instead of `1000`
- [ ] No `<style>` block remains in `<head>` (page-level `{% block head %}` untouched); all Jinja scripts/blocks intact
- [ ] Template still parses: `python3 -c "from jinja2 import Environment, FileSystemLoader; Environment(loader=FileSystemLoader('app/templates')).get_template('base.html')"` exits 0

**Verify:** the jinja parse command above → exit 0; `grep -c "b-nav {" static/css/site.css` → 0 (no vocabulary duplication); `grep -c "b-hamburger" static/css/site.css` → ≥1

**Steps:**

- [ ] **Step 1: Create `static/css/site.css`** with this header and the moved sections IN THIS ORDER (copy each line range verbatim from `app/templates/base.html` BEFORE editing it, unindented from template indentation):

```css
/* Vigilant site-specific styles.
   The shared b-* vocabulary lives in /static/ds/css/ (design-system/css in
   the repo). This file holds only what the design system deliberately does
   not cover, and loads AFTER it so responsive overrides win. */

/* ── Inline icon ── */                 /* from base.html 545-554 */
/* ── HTMX ── */                        /* from base.html 562-566 */
/* ── ESI banner override ── */         /* from base.html 572-578 */
/* ── Structure alert banners ── */     /* from base.html 579-617 */
/* ── Focus visibility (keyboard nav) ── */ /* from base.html 625-647 */
/* ── Mobile hamburger + menu ── */     /* from base.html 648-698 */
/* ── Responsive ── */                  /* from base.html 699-763 */
/* ── Hover utilities ── */             /* from base.html 771-802 */
```

(The comment markers above are placeholders for the actual copied rule blocks — the file must contain the real CSS from those ranges, not the comments alone.)

Two edits while moving:
1. Do NOT copy lines 764–770 (reduced-motion spin/pulse hold) — `motion.css`'s kill switch covers it.
2. In the focus-visibility block, keep everything verbatim (components.css only styles switch/check/modal-close focus; the site-wide `:focus-visible` policy is this file's job).

- [ ] **Step 2: Replace the style block in `base.html`**

Delete lines 13–807 (`<style nonce=…>` through `</style>`) and insert in their place:

```html
    <link rel="stylesheet" href="/static/ds/css/tokens.css">
    <link rel="stylesheet" href="/static/ds/css/motion.css">
    <link rel="stylesheet" href="/static/ds/css/components.css">
    <link rel="stylesheet" href="/static/css/site.css">
```

(Keep the existing tailwind link and everything else in `<head>` untouched.)

- [ ] **Step 3: notif-dropdown z-index**

In the notif dropdown inline style (search `id="notif-dropdown"`), change `z-index:1000` → `z-index:var(--z-dropdown)`.

- [ ] **Step 4: Verify**

```bash
python3 -c "from jinja2 import Environment, FileSystemLoader; Environment(loader=FileSystemLoader('app/templates')).get_template('base.html')"
grep -n "<style" app/templates/base.html   # expect: no match in head (page blocks excluded)
npx esbuild static/css/site.css --bundle --outdir=/tmp/css-check --allow-overwrite
```

- [ ] **Step 5: Commit**

```bash
git add static/css/site.css app/templates/base.html
git commit -m "feat(restyle): swap base.html inline CSS for design-system stylesheets + site.css"
```

---

### Task 3: Rebuild the login page with the ambient flythrough

**Goal:** `index.html` becomes the SSO login screen: ambient New Eden canvas behind a glass panel, real kill blips.

**Files:**
- Modify: `app/templates/index.html` (full rewrite of the content block)

**Acceptance Criteria:**
- [ ] Ambient module loads from `/static/ds/ambient/vigilant-ambient.js`, mounts on `document.body`, `systemsUrl: '/api/map/kspace-data/systems.json'`, `killSource: {type:'poll', url:'/api/ambient/kills', intervalMs:15000}`
- [ ] Glass panel uses `b-panel is-glass is-brackets` idiom; CTA is `b-btn is-primary`; error slot is `b-banner is-danger`
- [ ] No Tailwind `text-eve-*` / `bg-eve-*` classes remain in the template
- [ ] Script sits INSIDE `{% block content %}` with the CSP nonce
- [ ] Template parses (jinja check as in Task 2)

**Verify:** `python3 -c "from jinja2 import Environment, FileSystemLoader; Environment(loader=FileSystemLoader('app/templates')).get_template('index.html')"` → exit 0; `grep -c "text-eve\|bg-eve" app/templates/index.html` → 0

**Steps:**

- [ ] **Step 1: Rewrite `app/templates/index.html`**

```html
{% extends "base.html" %}
{% block title %}Vigilant — EVE Online Operations Tracker{% endblock %}
{% block content %}
<div style="min-height:calc(100vh - 200px);display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2rem;padding:1rem;">

    <div class="b-panel is-glass is-brackets" style="width:min(460px,100%);">
        <div style="padding:2.5rem 2.5rem 2rem;text-align:center;display:flex;flex-direction:column;gap:1rem;align-items:center;">
            <img src="/static/logo.png" alt="Vigilant" style="height:88px;width:auto;">
            <span class="b-nav-logo" style="font-size:18px;">VIGILANT</span>
            <span class="b-eyebrow">EVE Online Companion Dashboard</span>
        </div>
        <div style="padding:0 1.5rem;">
            <div class="b-row"><span class="b-row-label">Dashboard</span><span class="b-row-val is-muted">Characters, wallets, skill queues</span></div>
            <div class="b-row"><span class="b-row-label">Assets</span><span class="b-row-val is-muted">Search across all characters</span></div>
            <div class="b-row"><span class="b-row-label">Intel</span><span class="b-row-val is-muted">Kill feed, d-scan, local watch</span></div>
            <div class="b-row"><span class="b-row-label">Industry</span><span class="b-row-val is-muted">Jobs, blueprints, planetary</span></div>
        </div>
        {% if error %}
        <div style="padding:1rem 1.5rem 0;">
            <div class="b-banner is-danger" style="margin-bottom:0;">{{ error }}</div>
        </div>
        {% endif %}
        <div style="padding:1.5rem;display:flex;flex-direction:column;gap:0.75rem;align-items:center;">
            <a href="/auth/login" class="b-btn is-primary" style="width:100%;text-align:center;">Log in with EVE Online SSO</a>
            <span class="b-muted-sm" style="font-size:9px;letter-spacing:0.14em;text-transform:uppercase;">SSO · CCP authorized third-party · no passwords stored</span>
        </div>
    </div>

</div>

<script type="module" nonce="{{ request.state.csp_nonce }}">
    import { mount } from '/static/ds/ambient/vigilant-ambient.js';
    mount(document.body, {
        systemsUrl: '/api/map/kspace-data/systems.json',
        killSource: { type: 'poll', url: '/api/ambient/kills', intervalMs: 15000 }
    });
</script>
{% endblock %}
```

- [ ] **Step 2: Confirm CSP allows the module** — check `app/middleware/csp_nonce.py`: `script-src` must permit nonce'd scripts (it does — nonce pattern is the site standard). No change expected; if the module import is blocked in testing, note the CSP directive that needs `'self'` for module fetches (script-src already includes 'self' for htmx? htmx loads from unpkg — check the directive and mirror it).

- [ ] **Step 3: Verify + commit**

```bash
python3 -c "from jinja2 import Environment, FileSystemLoader; Environment(loader=FileSystemLoader('app/templates')).get_template('index.html')"
grep -c "text-eve\|bg-eve" app/templates/index.html   # expect 0
git add app/templates/index.html
git commit -m "feat(restyle): SSO login page with ambient New Eden flythrough"
```

---

### Task 4: Deploy + smoke verification (HUMAN GATE)

**Goal:** The restyle is live on vigilant.thunderborn.dev, verified working, with rollback rehearsed.

**Files:** none (operations)

**Acceptance Criteria:**
- [ ] Pre-deploy checklist run (CLAUDE.md): ast.parse on all changed .py; auth-surface change (two public endpoints) EXPLICITLY confirmed with the user before deploying; no DB schema changes (none in this plan)
- [ ] Deployed via `ssh thunderborn-home "/opt/vigilant/scripts/deploy.sh"`
- [ ] `docker logs vigilant-app-1` clean startup
- [ ] Smoke (curl + user eyeball): `/` (login page: glass panel, ambient canvas runs, SSO link), `/api/ambient/kills` returns JSON with cache header, `/static/ds/css/tokens.css` 200, dashboard + one htmx-heavy page (e.g. `/intel/kills`) render correctly, notif dropdown + mobile menu work
- [ ] User confirms the site looks right before the sweep begins

**Verify:** `curl -s -o /dev/null -w "%{http_code}" https://vigilant.thunderborn.dev/api/ambient/kills` → 200; user approval message

**Steps:**

- [ ] **Step 1:** Run pre-deploy checklist; get explicit user OK on the two public endpoints
- [ ] **Step 2:** `git push origin main` then `ssh thunderborn-home "/opt/vigilant/scripts/deploy.sh"` (push FIRST — deploy.sh pulls from GitHub; deploy-before-push runs old code)
- [ ] **Step 3:** `ssh thunderborn-home "docker logs vigilant-app-1 --since 5m 2>&1 | tail -30"` — no tracebacks
- [ ] **Step 4:** curl smoke set above; then hand to the user for the eyeball pass
- [ ] **Step 5:** If broken beyond quick fix: `ssh thunderborn-home "/opt/vigilant/scripts/rollback.sh"` and regroup (repo still has the commits; fix forward) (rollback.sh restores the whole :prev image — safe; never cherry-pick template-only changes onto older images.)

---

### Task 5: Full template sweep — checklist + batch A (core)

**Goal:** Sweep checklist covering all 111 templates; core pages verified and fixed.

**Files:**
- Create: `docs/superpowers/plans/sweep-checklist-2026-07-03.md` (generated: `ls app/templates/*.html app/templates/partials/*.html` grouped by nav section, one checkbox each)
- Modify: any core-batch template needing fixes

**Batch A pages:** `index, dashboard, intel, intel_kills, intel_kills_search, intel_dscan, intel_local, intel_watch, map, map_wormholes, characters (via dashboard), character_detail, admin` + their partials.

**Acceptance Criteria:**
- [ ] Checklist file lists all 111 templates with owner batch (A/B/C/D)
- [ ] Every batch-A page loaded on the live site: no unstyled/washed-out regions, dropdowns and htmx interactions work, no duplicate/conflicting page-local CSS (grep each template for `<style>` blocks and `--bg\|--accent` redefinitions)
- [ ] Fixes committed + redeployed at batch end (`git push` then deploy.sh)

**Verify:** checklist file shows batch A fully checked; user thumbs-up on the batch

**Steps** (repeat per page): load page → eyeball against the demo look → grep the template for page-local `<style>`/inline clashes → fix with `b-*`/token idiom → check page again → tick checklist. Commit batch, push, redeploy, verify.

---

### Task 6: Sweep batch B (industry/assets/corp)

Same procedure as Task 5. **Pages:** `assets, blueprints, industry, industry_jobs, mining, mining_ledger, journal, corporations, corp_contracts, corp_inventory, hauling, compression, appraisal` + partials. Same acceptance criteria/verify pattern; fixes committed + redeployed at batch end.

### Task 7: Sweep batch C (tools/fitting/planetary)

Same procedure. **Pages:** `fitting_tool, fitting_saved, fittings, ship_mastery, planetary, planetary_calculator, planetary_chain, planetary_lookup, gatecheck, dscan, dscan_view, discordtime, structure-age + remaining tools templates`. Fixes committed + redeployed at batch end.

### Task 8: Sweep batch D (remaining + partials) + close-out

Same procedure for all templates not covered by A–C (remaining root templates + `partials/` swept in context of their host pages). Close-out: checklist 111/111 ticked; update `.story`/memory notes if needed; final deploy; final report to user (pages fixed per batch, any deferred items).

---

## After the plan

Sub-project 2 complete = spec's goal delivered. Future work explicitly out of scope: star map restyle, site-wide Tailwind removal, SSE kill streaming.

## Self-review notes

- Spec coverage: mount+endpoint (T1), swap+site.css+z-index+margin (T2), login+ambient+poll (T3), big-bang deploy+smoke+rollback (T4), full sweep (T5–T8). Public-endpoint user confirmation embedded in T4 Step 1. Systems endpoint verified already public during planning (no auth dependency on `map_kspace_data`).
- Placeholders: T2 Step 1 uses explicit line-range copy instructions with the real source mapped in the disposition table (the repo is the source of truth for verbatim moves); T5–T8 sweep steps are procedures over live pages, inherently not code blocks.
- Consistency: `_recent_kill_systems(session, window_s)` used in both test and route; mount name `static_ds` unique; link order consistent between T2 criteria and steps.
- Known risk carried: `AsyncSessionLocal` import path unverified (T1 Step 4 NOTE tells the implementer to match the codebase); `Base.metadata.create_all` on SQLite may need the single-table fallback (T1 Step 1 NOTE).
```
