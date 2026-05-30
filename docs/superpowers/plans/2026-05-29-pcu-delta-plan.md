# PCU Delta Display Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show a 15-minute pilot count delta on the dashboard server bar, and add a live-polling PCU tile on the activity page.

**Architecture:** Two new/modified API endpoints in `dashboard.py` query `player_count_snapshots` for the delta; the dashboard JS reads the new field; the activity page uses an htmx-polled partial that re-arms its own 60s timer on each swap.

**Tech Stack:** FastAPI, SQLAlchemy async, Jinja2 templates, htmx

---

### Task 1: Backend — delta helper + extend api_server_status + add api_live_pcu

**Goal:** Two endpoints expose PCU delta data; one extends existing JSON, one renders a new HTML partial.

**Files:**
- Modify: `app/routes/dashboard.py` (lines 15, 732–746 area)

**Acceptance Criteria:**
- [ ] `GET /api/server-status` returns `delta_15m: int | null` (null when <15 min of ESI snapshots exist)
- [ ] `GET /api/live-pcu` returns HTML with current count and delta vs previous snapshot
- [ ] Both endpoints return gracefully when `player_count_snapshots` has no ESI rows

**Verify:** After deploy — `curl https://vigilant.thunderborn.dev/api/server-status` returns JSON with a `delta_15m` key; `curl https://vigilant.thunderborn.dev/api/live-pcu` returns an HTML fragment containing `live-pcu-tile`.

**Steps:**

- [ ] **Step 1: Add `PlayerCountSnapshot` to the models import in dashboard.py**

Current line 15 of `app/routes/dashboard.py`:
```python
from app.db.models import get_db, Character, CharacterDashboardCache, WalletSnapshot, CharacterAssetCache, CharacterCorpRoles, AsyncSessionLocal
```

Replace with:
```python
from app.db.models import get_db, Character, CharacterDashboardCache, WalletSnapshot, CharacterAssetCache, CharacterCorpRoles, AsyncSessionLocal, PlayerCountSnapshot
```

- [ ] **Step 2: Add `_fetch_15m_delta` helper above `fetch_server_status`**

Insert this function at line 731 (just before `async def fetch_server_status`):

```python
async def _fetch_15m_delta(db: AsyncSession) -> int | None:
    """Return latest ESI player count minus the snapshot from ~15 min ago.
    Returns None when fewer than 15 min of ESI data exist."""
    from sqlalchemy import desc as _desc
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    ref_cutoff = now - timedelta(minutes=14)

    latest = (await db.execute(
        select(PlayerCountSnapshot.player_count)
        .where(PlayerCountSnapshot.source == "esi")
        .order_by(_desc(PlayerCountSnapshot.recorded_at))
        .limit(1)
    )).scalar_one_or_none()

    if latest is None:
        return None

    ref = (await db.execute(
        select(PlayerCountSnapshot.player_count)
        .where(PlayerCountSnapshot.source == "esi")
        .where(PlayerCountSnapshot.recorded_at <= ref_cutoff)
        .order_by(_desc(PlayerCountSnapshot.recorded_at))
        .limit(1)
    )).scalar_one_or_none()

    return (latest - ref) if ref is not None else None
```

- [ ] **Step 3: Add `db` dependency to `api_server_status` and append delta**

Replace the existing `api_server_status` function (lines 744–746):
```python
@router.get("/api/server-status")
async def api_server_status():
    return JSONResponse(await fetch_server_status())
```

With:
```python
@router.get("/api/server-status")
async def api_server_status(db: AsyncSession = Depends(get_db)):
    status = await fetch_server_status()
    status["delta_15m"] = await _fetch_15m_delta(db)
    return JSONResponse(status)
```

- [ ] **Step 4: Add `api_live_pcu` endpoint after `api_server_status`**

Insert immediately after `api_server_status` (after the closing line of that function):

```python
@router.get("/api/live-pcu", response_class=HTMLResponse)
async def api_live_pcu(request: Request, db: AsyncSession = Depends(get_db)):
    """HTML partial: current ESI player count + delta vs previous snapshot.
    Swapped by htmx every 60s on the /tools/activity page."""
    from sqlalchemy import desc as _desc
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    stale_cutoff = now - timedelta(minutes=5)

    rows = (await db.execute(
        select(PlayerCountSnapshot.player_count, PlayerCountSnapshot.recorded_at)
        .where(PlayerCountSnapshot.source == "esi")
        .order_by(_desc(PlayerCountSnapshot.recorded_at))
        .limit(2)
    )).all()

    count = None
    delta = None
    if rows and rows[0].recorded_at >= stale_cutoff:
        count = rows[0].player_count
        delta = (count - rows[1].player_count) if len(rows) == 2 else None

    # Format for display
    count_str = None
    if count is not None:
        count_str = f"{count / 1000:.1f}K" if count >= 1000 else str(count)

    return templates.TemplateResponse(
        request,
        "partials/live_pcu_tile.html",
        {"count_str": count_str, "delta": delta},
    )
```

- [ ] **Step 5: Syntax-check the modified file**

```bash
python3 -c "import ast; ast.parse(open('app/routes/dashboard.py').read())" && echo "OK"
```
Expected: `OK`

- [ ] **Step 6: Commit**

```bash
git add app/routes/dashboard.py
git commit -m "feat(pcu): add 15m delta to server-status + live-pcu endpoint"
```

---

### Task 2: Template — create live_pcu_tile.html partial

**Goal:** The htmx swap target partial that renders the live PCU tile and re-arms its own 60s poll on every swap.

**Files:**
- Create: `app/templates/partials/live_pcu_tile.html`

**Acceptance Criteria:**
- [ ] Partial wraps content in `id="live-pcu-tile"` div with `hx-get="/api/live-pcu" hx-trigger="every 60s" hx-swap="outerHTML"` so the timer re-arms after each swap
- [ ] Shows formatted count when data is available; `—` (muted) when count is None
- [ ] Shows `+N` in green or `-N` in red when delta is non-zero; no badge when delta is 0 or None

**Verify:** After deploy — load `/tools/activity`, open DevTools Network tab, confirm a request to `/api/live-pcu` fires every ~60s and returns a fragment containing `id="live-pcu-tile"`.

**Steps:**

- [ ] **Step 1: Create the partial file**

Create `app/templates/partials/live_pcu_tile.html` with this content:

```html
{# Live PCU tile — htmx outerHTML swap. Re-arms own 60s poll on every swap. #}
<div id="live-pcu-tile"
     hx-get="/api/live-pcu"
     hx-trigger="every 60s"
     hx-swap="outerHTML">
    <div class="ta-stat">
        <div class="ta-stat-val">
            {% if count_str is not none %}
                {{- count_str -}}
                {% if delta is not none and delta != 0 %}
                <span style="font-size:11px;font-weight:400;margin-left:0.3rem;color:{% if delta > 0 %}var(--success){% else %}var(--danger){% endif %};">
                    {%- if delta > 0 %}+{% endif %}{{ '{:,}'.format(delta) -}}
                </span>
                {% endif %}
            {% else %}
                <span style="color:var(--muted)">—</span>
            {% endif %}
        </div>
        <div class="ta-stat-label">Live pilots</div>
    </div>
</div>
```

- [ ] **Step 2: Commit**

```bash
git add app/templates/partials/live_pcu_tile.html
git commit -m "feat(pcu): add live_pcu_tile partial for htmx polling"
```

---

### Task 3: Dashboard JS — render delta badge on pilots count

**Goal:** `updateServerStatus()` reads `data.delta_15m` from the API and appends a colored `+N` / `-N` badge next to the pilot count.

**Files:**
- Modify: `app/templates/dashboard.html` (lines ~499–527)

**Acceptance Criteria:**
- [ ] A green `+N` badge appears when `delta_15m > 0`
- [ ] A red `-N` badge appears when `delta_15m < 0`
- [ ] No badge when delta is `0`, `null`, or the server is offline
- [ ] Existing pilots count, online dot, and EVE time still work correctly

**Verify:** After deploy — open the dashboard, open DevTools console, run `updateServerStatus()` manually, confirm the `#server-players` element contains a colored delta span when the API returns a non-zero `delta_15m`.

**Steps:**

- [ ] **Step 1: Replace the `updateServerStatus` body in dashboard.html**

Find this block (around lines 499–527):

```javascript
function updateServerStatus() {
    fetch('/api/server-status')
        .then(r => r.json())
        .then(data => {
            const dot = document.getElementById('server-status-dot');
            const text = document.getElementById('server-status-text');
            const players = document.getElementById('server-players');
            if (data.online) {
                dot.style.background = 'var(--success)';
                text.style.color = 'var(--success)';
                text.textContent = 'TRANQUILITY — ONLINE';
                if (data.players) {
                    players.textContent = data.players.toLocaleString() + ' pilots online';
                    players.style.display = '';
                }
            } else {
                dot.style.background = 'var(--danger)';
                text.style.color = 'var(--danger)';
                text.textContent = 'TRANQUILITY — OFFLINE';
                players.style.display = 'none';
            }
        })
        .catch(() => {
            document.getElementById('server-status-text').textContent = 'TRANQUILITY — UNKNOWN';
        });
}
```

Replace with:

```javascript
function updateServerStatus() {
    fetch('/api/server-status')
        .then(r => r.json())
        .then(data => {
            const dot = document.getElementById('server-status-dot');
            const text = document.getElementById('server-status-text');
            const players = document.getElementById('server-players');
            if (data.online) {
                dot.style.background = 'var(--success)';
                text.style.color = 'var(--success)';
                text.textContent = 'TRANQUILITY — ONLINE';
                if (data.players) {
                    players.innerHTML = '';
                    const main = document.createElement('span');
                    main.textContent = data.players.toLocaleString() + ' pilots online';
                    players.appendChild(main);
                    const d = data.delta_15m;
                    if (d !== null && d !== undefined && d !== 0) {
                        const badge = document.createElement('span');
                        badge.textContent = (d > 0 ? '+' : '') + d.toLocaleString();
                        badge.style.cssText = 'margin-left:0.5rem;font-size:10px;color:' + (d > 0 ? 'var(--success)' : 'var(--danger)');
                        players.appendChild(badge);
                    }
                    players.style.display = '';
                }
            } else {
                dot.style.background = 'var(--danger)';
                text.style.color = 'var(--danger)';
                text.textContent = 'TRANQUILITY — OFFLINE';
                players.style.display = 'none';
            }
        })
        .catch(() => {
            document.getElementById('server-status-text').textContent = 'TRANQUILITY — UNKNOWN';
        });
}
```

- [ ] **Step 2: Commit**

```bash
git add app/templates/dashboard.html
git commit -m "feat(pcu): show 15m delta badge on dashboard pilots count"
```

---

### Task 4: Activity page — add live tile placeholder + widen stat grid

**Goal:** The `/tools/activity` page gains a fifth "Live pilots" tile that htmx loads immediately and refreshes every 60s.

**Files:**
- Modify: `app/templates/tools_activity.html` (lines ~68–85)

**Acceptance Criteria:**
- [ ] Stat grid is 5-column (`repeat(5,1fr)`)
- [ ] A live tile placeholder is present that triggers `hx-get="/api/live-pcu"` on `load` and then every 60s
- [ ] The tile is visible at all window sizes (no overflow — mobile responsiveness is not a concern here; existing `.ta-stats` wraps on small screens)

**Verify:** After deploy — navigate to `/tools/activity`, confirm 5 stat tiles are visible, confirm the live tile shows a pilot count within ~5s of page load, confirm it updates ~60s later.

**Steps:**

- [ ] **Step 1: Widen the stat grid and add the live tile placeholder**

Find this block in `app/templates/tools_activity.html` (lines ~68–85):

```html
        <div class="ta-stats" style="grid-template-columns:repeat(4,1fr);">
            <div class="ta-stat">
                <div class="ta-stat-val">{{ fmt_pcu(peak_pcu) }}</div>
                <div class="ta-stat-label">Peak players</div>
            </div>
            <div class="ta-stat">
                <div class="ta-stat-val">{{ fmt_pcu(mean_pcu) }}</div>
                <div class="ta-stat-label">Mean players</div>
            </div>
            <div class="ta-stat">
                <div class="ta-stat-val">{{ fmt_pcu(total_kills) }}</div>
                <div class="ta-stat-label">Kills in window</div>
            </div>
            <div class="ta-stat">
                <div class="ta-stat-val">{{ fmt_isk(total_isk) }}</div>
                <div class="ta-stat-label">ISK destroyed</div>
            </div>
        </div>
```

Replace with:

```html
        <div class="ta-stats" style="grid-template-columns:repeat(5,1fr);">
            <div class="ta-stat">
                <div class="ta-stat-val">{{ fmt_pcu(peak_pcu) }}</div>
                <div class="ta-stat-label">Peak players</div>
            </div>
            <div class="ta-stat">
                <div class="ta-stat-val">{{ fmt_pcu(mean_pcu) }}</div>
                <div class="ta-stat-label">Mean players</div>
            </div>
            <div class="ta-stat">
                <div class="ta-stat-val">{{ fmt_pcu(total_kills) }}</div>
                <div class="ta-stat-label">Kills in window</div>
            </div>
            <div class="ta-stat">
                <div class="ta-stat-val">{{ fmt_isk(total_isk) }}</div>
                <div class="ta-stat-label">ISK destroyed</div>
            </div>
            <div id="live-pcu-tile"
                 hx-get="/api/live-pcu"
                 hx-trigger="load, every 60s"
                 hx-swap="outerHTML">
                <div class="ta-stat">
                    <div class="ta-stat-val" style="color:var(--muted)">—</div>
                    <div class="ta-stat-label">Live pilots</div>
                </div>
            </div>
        </div>
```

- [ ] **Step 2: Commit**

```bash
git add app/templates/tools_activity.html
git commit -m "feat(pcu): add live pilots tile to activity page"
```

---

### Task 5: Deploy and smoke-test

**Goal:** All four changes are live on the VPS and verified working.

**Files:** None — deploy only.

**Acceptance Criteria:**
- [ ] `docker logs vigilant-app-1` shows no startup errors after deploy
- [ ] Dashboard server bar shows `N pilots online +M` or `-M` with correct color
- [ ] `/tools/activity` shows 5 stat tiles; Live pilots tile populates within 5s
- [ ] Live tile refreshes approximately every 60s (verify via DevTools Network)
- [ ] `curl /api/server-status` includes `delta_15m` key in JSON

**Verify:** `ssh ijohnson@146.190.140.112 "docker logs vigilant-app-1 --tail 20"` — no ERROR lines.

**Steps:**

- [ ] **Step 1: Push all commits**

```bash
git push origin main
```

- [ ] **Step 2: Deploy**

```bash
ssh ijohnson@146.190.140.112 "/opt/vigilant/scripts/deploy.sh"
```

- [ ] **Step 3: Check logs**

```bash
ssh ijohnson@146.190.140.112 "docker logs vigilant-app-1 --tail 30"
```

Expected: app startup messages, no ERROR or Traceback lines.

- [ ] **Step 4: Verify server-status JSON**

```bash
curl -s https://vigilant.thunderborn.dev/api/server-status | python3 -m json.tool
```

Expected output includes `"delta_15m": <int or null>`.

- [ ] **Step 5: Verify live-pcu endpoint**

```bash
curl -s https://vigilant.thunderborn.dev/api/live-pcu
```

Expected: HTML fragment containing `id="live-pcu-tile"` and `Live pilots` text.

- [ ] **Step 6: Browser smoke-test**

Navigate to `https://vigilant.thunderborn.dev/dashboard` — confirm the server bar shows pilot count with optional colored delta.

Navigate to `https://vigilant.thunderborn.dev/tools/activity` — confirm 5 stat tiles are visible, Live pilots tile populates quickly.
