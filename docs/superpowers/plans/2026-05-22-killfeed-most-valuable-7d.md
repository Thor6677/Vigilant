# Kill Feed — Most Valuable Last 7 Days · Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a collapsible "Most Valuable · Last 7 Days" strip at the top of `/intel/kills`, with six-card Structures + Ships rows, SWR-cached server-side, click-to-expand the existing kill detail panel inline.

**Architecture:** Backend SWR cache pattern proven on `/dashboard/kill-pulse` — cache the context dict 5 minutes, refresh in background after expiry. Single new route `/intel/kills/top` returns the rendered partial. Frontend strip is a self-contained block above the existing filter bar; cards open a detail panel in their own slot (independent of feed-row detail panels). No changes to the live-feed JS, ingestion pipeline, or existing routes.

**Tech Stack:** FastAPI + SQLAlchemy async + Jinja2 templates + htmx (existing app stack). No new dependencies.

**Vigilant-specific conventions (read once):**
- Always rebuild via `/opt/vigilant/scripts/deploy.sh` — `docker compose restart` alone doesn't apply code/template changes (per CLAUDE.md).
- Pre-deploy mandatory: syntax-check every modified `.py` with `python3 -c "import ast; ast.parse(open(F).read())"`.
- Jinja2 dict access: use `d['key']`, not `d.key` (CLAUDE.md gotcha).
- Cache the context **dict**, not the rendered HTML — CSP nonce rotates per request (`feedback_swr_panel_caching`).
- Indexes added to existing tables don't auto-deploy via `create_all` — use `CREATE INDEX IF NOT EXISTS` (`feedback_create_all_skips_indexes`).
- AsyncSession is **not** safe for concurrent statements; run the two queries sequentially under one session (CLAUDE.md async-session gotcha).

**File structure:**

| File | Action | Responsibility |
|---|---|---|
| `app/routes/intel_kills.py` | Modify | Add SWR cache module-globals, `_compute_top_context`, `_refresh_top_background`, `/intel/kills/top` handler |
| `app/templates/partials/intel_kills_top.html` | Create | Strip partial — header + two card grids + detail slot |
| `app/templates/intel_kills.html` | Modify | Insert strip wrapper above `.kf-filters`; add CSS for `.kf-top-*`; add chevron-toggle + card-click JS |

---

### Task 1: Backend route + partial template

**Goal:** A working `GET /intel/kills/top` endpoint that returns the rendered strip partial (Structures + Ships, six cards each, ordered by ISK destroyed over the last 7 days, SWR-cached 5 min).

**Files:**
- Modify: `app/routes/intel_kills.py` — add imports + module globals + 3 functions + route
- Create: `app/templates/partials/intel_kills_top.html` — strip partial

**Acceptance Criteria:**
- [ ] `GET /intel/kills/top` returns HTML for the strip when authenticated; 401 (empty body) when not.
- [ ] Cold-path response renders Structures (category_id=65) and Ships (category_id=6) sections, each with up to 6 cards, ordered by `total_value` DESC across the last 7 days.
- [ ] Warm-path (cached) response returns within ~20ms with the same content as the most recent cold-path.
- [ ] After cache TTL elapses, the next request returns the stale cached value AND triggers a background refresh exactly once (in-flight set prevents duplicate refreshes).
- [ ] Empty categories render "No structure kills in the last 7 days." / "No ship kills in the last 7 days." centered in muted text — per-section, so an empty Structures row doesn't suppress a populated Ships row.

**Verify:**
1. `python3 -c "import ast; ast.parse(open('app/routes/intel_kills.py').read())"` → no output, exit 0.
2. Deploy: `ssh ijohnson@146.190.140.112 "/opt/vigilant/scripts/deploy.sh"` → completes without errors.
3. App boots cleanly: `ssh ijohnson@146.190.140.112 "docker logs --tail 100 vigilant-app-1 2>&1 | grep -iE 'error|traceback'"` → no new tracebacks.
4. Endpoint serves: `ssh ijohnson@146.190.140.112 "curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8000/intel/kills/top"` → `401` (no session) — confirms route is wired.
5. From a logged-in browser, navigate to `/intel/kills/top` directly — see HTML with `kf-top-header` and at least one `kf-top-card` row.

**Steps:**

- [ ] **Step 1: Add imports + module-global SWR state to `app/routes/intel_kills.py`**

Four narrow additions to the existing import block (around lines 10–28). Do **not** rewrite the block — add only what's missing:

1. Add a top-level `import asyncio` (alongside the existing `import logging`).
2. Extend the `from datetime import datetime, timedelta` line to `from datetime import datetime, timedelta, timezone`.
3. Extend the `from app.db.models import Character, Killmail, KillmailAttacker, KillmailItem, get_db` line to add `AsyncSessionLocal` (alphabetical order keeps it tidy):

```python
from app.db.models import (
    AsyncSessionLocal,
    Character,
    Killmail,
    KillmailAttacker,
    KillmailItem,
    get_db,
)
```

4. Extend `from app.db.sde_models import SDESystem` to `from app.db.sde_models import SDESystem, SDEType`.

Then add the module-global SWR state immediately AFTER the existing `_BAND_NORMALIZE` block (the dict ending around line 44) and BEFORE the `@router.get("/intel/kills"` handler:

```python
# ── Most Valuable last-7-days strip — SWR cache state ──────────────────
# Universe-wide, identical for every authenticated viewer → single global key.
# 5-min TTL matches the dashboard kill-pulse / corp-stats panels
# (feedback_swr_panel_caching). Cache the context dict, not the rendered HTML,
# so CSP nonce rotation doesn't ghost the cached page.
_top_cache: dict[str, dict] = {}
_top_revalidating: set[str] = set()
_TOP_TTL = 300  # seconds
```

- [ ] **Step 2: Add the ISK formatter helper**

Insert immediately after the `_top_revalidating` line:

```python
def _fmt_top_isk(v: float | None) -> str:
    """Format ISK for the Most Valuable cards — two decimals, suffixed unit.
    Matches zKillboard's '33.44b' style but with uppercase B / T / M for
    consistency with the existing kill feed's `kf-isk` output.
    """
    v = float(v or 0)
    if v >= 1e12:
        return f"{v/1e12:.2f}T"
    if v >= 1e9:
        return f"{v/1e9:.2f}B"
    if v >= 1e6:
        return f"{v/1e6:.2f}M"
    return f"{v:,.0f}"
```

- [ ] **Step 3: Add `_compute_top_context`**

Insert immediately after `_fmt_top_isk`:

```python
async def _compute_top_context(db: AsyncSession) -> dict:
    """Query last-7-days top-6 structures (category_id=65) + top-6 ships
    (category_id=6) by total_value. Resolve type / corp / alliance / system
    names. Return template context dict.

    Two SELECTs run **sequentially** on a single AsyncSession — concurrent
    statements on one session are unsafe (CLAUDE.md async-session gotcha)
    and these queries are cheap enough that sequential is fine.
    """
    cutoff = datetime.utcnow() - timedelta(days=7)

    async def _query_for_category(category_id: int) -> list[Killmail]:
        stmt = (
            select(Killmail)
            .join(SDEType, SDEType.type_id == Killmail.victim_ship_type_id)
            .where(SDEType.category_id == category_id)
            .where(Killmail.killmail_time >= cutoff)
            .where(Killmail.total_value.is_not(None))
            .order_by(Killmail.total_value.desc())
            .limit(6)
        )
        return list((await db.execute(stmt)).scalars().all())

    structures = await _query_for_category(65)
    ships = await _query_for_category(6)

    all_kills = structures + ships
    if not all_kills:
        return {"structures": [], "ships": []}

    type_ids: set[int] = {k.victim_ship_type_id for k in all_kills if k.victim_ship_type_id}
    entity_ids: set[int] = set()
    system_ids: set[int] = set()
    for k in all_kills:
        if k.victim_corporation_id:
            entity_ids.add(k.victim_corporation_id)
        if k.victim_alliance_id:
            entity_ids.add(k.victim_alliance_id)
        if k.solar_system_id:
            system_ids.add(k.solar_system_id)

    type_names = await type_ids_to_names(db, list(type_ids)) if type_ids else {}
    entity_names = await resolve_entity_names(list(entity_ids)) if entity_ids else {}

    sys_map: dict[int, dict] = {}
    if system_ids:
        sys_q = select(
            SDESystem.system_id, SDESystem.system_name, SDESystem.security
        ).where(SDESystem.system_id.in_(system_ids))
        for sid, name, sec in (await db.execute(sys_q)).all():
            sys_map[sid] = {"name": name, "security": sec}

    def _band(sid: int) -> str:
        meta = _sys_meta_cache.get(sid)
        if meta:
            return _BAND_NORMALIZE.get(meta.get("band") or "Unknown", "unknown")
        sys = sys_map.get(sid)
        if not sys or sys["security"] is None:
            return "unknown"
        if sid >= 31000000:
            return "wh"
        if sys["security"] >= 0.5:
            return "hs"
        if sys["security"] > 0.0:
            return "ls"
        return "ns"

    def _card(k: Killmail) -> dict:
        return {
            "killmail_id": k.killmail_id,
            "type_id": k.victim_ship_type_id,
            "type_name": type_names.get(k.victim_ship_type_id, f"#{k.victim_ship_type_id}"),
            "isk_fmt": _fmt_top_isk(k.total_value),
            "victim_corp": (
                entity_names.get(k.victim_corporation_id, "")
                if k.victim_corporation_id else ""
            ),
            "victim_alliance": (
                entity_names.get(k.victim_alliance_id, "")
                if k.victim_alliance_id else ""
            ),
            "system_id": k.solar_system_id,
            "system_name": (sys_map.get(k.solar_system_id) or {}).get(
                "name", f"#{k.solar_system_id}"
            ),
            "system_band": _band(k.solar_system_id),
        }

    return {
        "structures": [_card(k) for k in structures],
        "ships": [_card(k) for k in ships],
    }
```

- [ ] **Step 4: Add `_refresh_top_background`**

Insert immediately after `_compute_top_context`:

```python
async def _refresh_top_background() -> None:
    """SWR background refresh — own session per feedback_swr_panel_caching."""
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

- [ ] **Step 5: Add the `/intel/kills/top` route handler**

Insert immediately after `_refresh_top_background` (anywhere before the older-mode and detail-panel route handlers is fine — placing it near the page-shell handler keeps related routes together):

```python
@router.get("/intel/kills/top", response_class=HTMLResponse)
async def intel_kills_top(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Most Valuable last 7 days — Structures + Ships strip.

    Universe-wide aggregation. Stale-while-revalidate cached server-side:
    return cached dict instantly, refresh in background after TTL.
    """
    user_id = request.session.get("user_id")
    if not user_id:
        return HTMLResponse("", status_code=401)

    key = "v1"
    now = datetime.now(timezone.utc)
    cached = _top_cache.get(key)
    if cached:
        if cached["expires_at"] <= now and key not in _top_revalidating:
            _top_revalidating.add(key)
            asyncio.create_task(_refresh_top_background())
        ctx = cached["context"]
    else:
        ctx = await _compute_top_context(db)
        _top_cache[key] = {
            "context": ctx,
            "expires_at": now + timedelta(seconds=_TOP_TTL),
        }

    return templates.TemplateResponse(
        "partials/intel_kills_top.html",
        {"request": request, **ctx},
    )
```

- [ ] **Step 6: Create `app/templates/partials/intel_kills_top.html`**

Write the file with this exact content:

```html
<div class="kf-top-header">
  <button type="button" id="kf-top-toggle" class="kf-top-toggle" aria-expanded="true">
    <span class="kf-top-chev">▾</span> MOST VALUABLE · LAST 7 DAYS
  </button>
</div>

<div id="kf-top-body">
  <section class="kf-top-section">
    <div class="kf-top-section-label">Structures</div>
    {% if structures %}
    <div class="kf-top-grid">
      {% for c in structures %}
      <div class="kf-top-card" data-kid="{{ c['killmail_id'] }}">
        <img class="kf-top-img" loading="lazy"
             src="https://images.evetech.net/types/{{ c['type_id'] }}/render?size=128"
             width="96" height="96" alt="{{ c['type_name'] }}"
             data-on-error="hide">
        <div class="kf-top-type">{{ c['type_name'] }}</div>
        <div class="kf-top-isk">{{ c['isk_fmt'] }}</div>
        <div class="kf-top-meta kf-sys-{{ c['system_band'] }}">{{ c['system_name'] }}</div>
        <div class="kf-top-corp">{{ c['victim_corp'] or c['victim_alliance'] or '—' }}</div>
      </div>
      {% endfor %}
    </div>
    {% else %}
    <div class="kf-top-empty">No structure kills in the last 7 days.</div>
    {% endif %}
  </section>

  <section class="kf-top-section">
    <div class="kf-top-section-label">Ships</div>
    {% if ships %}
    <div class="kf-top-grid">
      {% for c in ships %}
      <div class="kf-top-card" data-kid="{{ c['killmail_id'] }}">
        <img class="kf-top-img" loading="lazy"
             src="https://images.evetech.net/types/{{ c['type_id'] }}/render?size=128"
             width="96" height="96" alt="{{ c['type_name'] }}"
             data-on-error="hide">
        <div class="kf-top-type">{{ c['type_name'] }}</div>
        <div class="kf-top-isk">{{ c['isk_fmt'] }}</div>
        <div class="kf-top-meta kf-sys-{{ c['system_band'] }}">{{ c['system_name'] }}</div>
        <div class="kf-top-corp">{{ c['victim_corp'] or c['victim_alliance'] or '—' }}</div>
      </div>
      {% endfor %}
    </div>
    {% else %}
    <div class="kf-top-empty">No ship kills in the last 7 days.</div>
    {% endif %}
  </section>
</div>

<div id="kf-top-detail-slot"></div>
```

(Jinja2 dict access uses `c['key']` not `c.key` — see CLAUDE.md gotcha. `data-on-error="hide"` matches the existing convention in `dashboard.html` for graceful image-fallback.)

- [ ] **Step 7: Syntax check + commit**

```bash
python3 -c "import ast; ast.parse(open('app/routes/intel_kills.py').read())"
git add app/routes/intel_kills.py app/templates/partials/intel_kills_top.html
git commit -m "feat(kills): /intel/kills/top route — last-7-days top structures + ships, SWR-cached

Adds the backend half of Feature A from
docs/superpowers/specs/2026-05-22-killfeed-most-valuable-7d-design.md.

Returns the Most Valuable strip partial. SWR-cached 5 min via the same
pattern as /dashboard/kill-pulse (feedback_swr_panel_caching). Wiring
into intel_kills.html in the next commit."
```

Expected: clean commit, no pre-commit hook failures.

- [ ] **Step 8: Deploy + smoke-test the endpoint**

Deploy and verify:

```bash
ssh ijohnson@146.190.140.112 "/opt/vigilant/scripts/deploy.sh"
```

Expected: deploy completes; container is healthy.

```bash
ssh ijohnson@146.190.140.112 "docker logs --tail 80 vigilant-app-1 2>&1 | grep -iE 'error|traceback' | head"
```

Expected: no new ERROR or Traceback lines tied to the deploy.

```bash
ssh ijohnson@146.190.140.112 "curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8000/intel/kills/top"
```

Expected: `401` (no session cookie; route is wired and returns the auth-rejected empty body).

Then from a logged-in browser, navigate directly to `https://vigilant.thunderborn.dev/intel/kills/top` — you should see the raw partial HTML (no surrounding chrome). Confirm:
- `kf-top-header` block visible.
- Six (or fewer) `kf-top-card` divs under Structures.
- Six (or fewer) `kf-top-card` divs under Ships.
- ISK formatted with `B` / `T` suffix.
- Corp / alliance names populated (not raw IDs like `#98000123`).

If type or entity names show as `#<id>`, the name-resolution caches are cold — reload once; the SWR refresh will populate. If the issue persists, see Task 3's verification steps for the `sde_types.category_id` check.

---

### Task 2: Page integration — strip wrapper, CSS, JS

**Goal:** The strip renders at the top of `/intel/kills`. Chevron collapses/expands and persists in localStorage. Clicking a card opens the existing kill-detail panel in `#kf-top-detail-slot`. Re-clicking the same card collapses. Clicking a different card swaps.

**Files:**
- Modify: `app/templates/intel_kills.html` — insert strip wrapper above `.kf-filters`; add CSS for `.kf-top-*` classes; add chevron + card-click JS

**Acceptance Criteria:**
- [ ] `/intel/kills` loads with the Most Valuable strip rendered above the filter rows, lazy-loaded via htmx after the page shell paints.
- [ ] Chevron click toggles `#kf-top-body` visibility; state persists across page reloads via `localStorage['vigilant:kf:topstrip']`.
- [ ] Clicking a structure or ship card fetches `/intel/kills/{killmail_id}/detail` and renders it into `#kf-top-detail-slot` with the slide-down animation. The card gets the `.open` highlight class.
- [ ] Re-clicking the same card collapses the detail panel and clears the slot.
- [ ] Clicking a different card while another is open swaps the panel content cleanly (no double-render, no leftover highlight on the previous card).
- [ ] The existing live feed and its detail panels continue to work unchanged — opening a card detail does NOT close any detail open in the feed below, and vice versa.

**Verify:**
1. `python3 -c "import ast; ast.parse(open('app/templates/intel_kills.html').read())"` — wait, that's HTML/Jinja, not Python. Instead: `python3 -c "from jinja2 import Environment, FileSystemLoader; Environment(loader=FileSystemLoader('app/templates')).get_template('intel_kills.html')"` → no `TemplateSyntaxError`.
2. Deploy + browser smoke test (detailed in Step 5 below).
3. Manual interaction tests pass (collapse/expand, card click, detail open/close/swap).

**Steps:**

- [ ] **Step 1: Insert the strip wrapper into `intel_kills.html`**

Open `app/templates/intel_kills.html`. Find the section head block (around lines 113–117):

```html
<div class="b-section">
  <div class="b-section-head kf-head">
    <span class="kf-title">KILL FEED</span>
    <span class="kf-live" id="kf-status">LIVE · 1h · — kills</span>
  </div>

  <div class="kf-filters">
```

Insert the strip wrapper between `</div>` (end of section head) and `<div class="kf-filters">`:

```html
<div class="b-section">
  <div class="b-section-head kf-head">
    <span class="kf-title">KILL FEED</span>
    <span class="kf-live" id="kf-status">LIVE · 1h · — kills</span>
  </div>

  <div id="kf-top-strip-wrap"
       hx-get="/intel/kills/top"
       hx-trigger="load"
       hx-swap="innerHTML"></div>

  <div class="kf-filters">
```

- [ ] **Step 2: Add CSS for the strip**

Find the end of the existing `<style nonce="{{ request.state.csp_nonce }}">` block in `intel_kills.html` (around line 109, just before `</style>`). Insert the following CSS rules immediately before the closing `</style>`:

```css
  /* ── Most Valuable last-7-days strip ──────────────────────────────── */
  #kf-top-strip-wrap { margin-bottom:10px; }
  .kf-top-header { padding:10px 0 6px; }
  .kf-top-toggle { background:none; border:none; color:var(--text); font-size:13px; letter-spacing:0.16em; font-weight:600; padding:0; cursor:pointer; display:flex; align-items:center; gap:6px; }
  .kf-top-toggle .kf-top-chev { display:inline-block; transition:transform 0.2s; }
  .kf-top-toggle[aria-expanded="false"] .kf-top-chev { transform:rotate(-90deg); }
  #kf-top-body { padding-bottom:10px; border-bottom:1px solid var(--border); }
  #kf-top-body.collapsed { display:none; }
  .kf-top-section { margin-bottom:10px; }
  .kf-top-section-label { font-size:9px; color:var(--muted); text-transform:uppercase; letter-spacing:0.14em; margin-bottom:6px; }
  .kf-top-grid { display:grid; grid-template-columns:repeat(6, 1fr); gap:8px; }
  @media (max-width: 900px) { .kf-top-grid { grid-template-columns:repeat(3, 1fr); } }
  .kf-top-card { background:var(--surface); border:1px solid var(--border); padding:8px; text-align:center; cursor:pointer; transition:background-color 0.2s, border-color 0.2s; border-radius:3px; min-width:0; }
  .kf-top-card:hover { background:rgba(94,177,255,0.06); border-color:var(--accent); }
  .kf-top-card.open { background:rgba(94,177,255,0.08); border-color:var(--accent); }
  .kf-top-img { width:96px; height:96px; object-fit:contain; }
  .kf-top-type { font-size:10px; color:var(--accent); margin-top:4px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .kf-top-isk { font-size:13px; color:#facc15; font-weight:600; margin:4px 0 2px; font-family:"SF Mono",Menlo,monospace; }
  .kf-top-meta { font-size:10px; color:var(--muted); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .kf-top-corp { font-size:9px; color:var(--muted); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; margin-top:2px; }
  .kf-top-empty { color:var(--muted); font-size:11px; padding:8px 0; text-align:center; }
  #kf-top-detail-slot { margin-bottom:10px; }
```

- [ ] **Step 3: Add the chevron + card-click JS**

Find the second `<script nonce="{{ request.state.csp_nonce }}">` block in `intel_kills.html` (the long IIFE that starts around line 191 with `(function() { var STORAGE_KEY = 'vigilant:kf:filters';`). Add a NEW separate `<script>` block AFTER the existing closing `</script>` and BEFORE `{% endblock %}` so the new code is isolated from the existing live-feed IIFE.

The new block is its own IIFE:

```html
<script nonce="{{ request.state.csp_nonce }}">
  (function() {
    var TOP_KEY = 'vigilant:kf:topstrip';

    function applyTopState() {
      var collapsed = false;
      try { collapsed = localStorage.getItem(TOP_KEY) === 'collapsed'; } catch (e) {}
      var body = document.getElementById('kf-top-body');
      var toggle = document.getElementById('kf-top-toggle');
      if (body) body.classList.toggle('collapsed', collapsed);
      if (toggle) toggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
    }

    function bindTopToggle() {
      var toggle = document.getElementById('kf-top-toggle');
      if (!toggle || toggle.dataset.bound === '1') return;
      toggle.dataset.bound = '1';
      toggle.addEventListener('click', function() {
        var body = document.getElementById('kf-top-body');
        if (!body) return;
        var collapsed = body.classList.toggle('collapsed');
        toggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
        try { localStorage.setItem(TOP_KEY, collapsed ? 'collapsed' : 'expanded'); } catch (e) {}
      });
    }

    function bindTopCards() {
      var cards = document.querySelectorAll('#kf-top-strip-wrap .kf-top-card');
      cards.forEach(function(card) {
        if (card.dataset.bound === '1') return;
        card.dataset.bound = '1';
        card.addEventListener('click', function() {
          var kid = card.dataset.kid;
          var slot = document.getElementById('kf-top-detail-slot');
          if (!slot) return;

          // If this card is already open, animated-collapse and clear.
          if (card.classList.contains('open')) {
            var openPanel = slot.querySelector('.kf-detail');
            card.classList.remove('open');
            if (openPanel) {
              openPanel.classList.remove('shown');
              setTimeout(function() { slot.innerHTML = ''; }, 320);
            } else {
              slot.innerHTML = '';
            }
            return;
          }

          // Different card opening — clear any prior highlight.
          document.querySelectorAll('#kf-top-strip-wrap .kf-top-card.open').forEach(function(c) {
            c.classList.remove('open');
          });
          card.classList.add('open');

          fetch('/intel/kills/' + kid + '/detail')
            .then(function(r) { return r.text(); })
            .then(function(html) {
              slot.innerHTML = html;
              var panel = slot.querySelector('.kf-detail');
              if (panel) {
                // Force reflow so the slide-in transition fires.
                void panel.offsetHeight;
                requestAnimationFrame(function() { panel.classList.add('shown'); });
              }
            })
            .catch(function() { card.classList.remove('open'); });
        });
      });
    }

    function refreshTopWiring() {
      applyTopState();
      bindTopToggle();
      bindTopCards();
    }

    // Initial bind (covers the case where htmx swap already happened
    // before this script ran — defensive).
    refreshTopWiring();

    // Rebind after htmx loads/refreshes the strip partial.
    document.addEventListener('htmx:afterSwap', function(e) {
      if (e.target && e.target.id === 'kf-top-strip-wrap') {
        refreshTopWiring();
      }
    });
  })();
</script>
```

(This block is placed in the **content** block per the CLAUDE.md "Jinja2 script blocks" gotcha — content after `{% endblock %}` is discarded. The existing live-feed `<script>` blocks already live inside `{% block content %}`; place the new block alongside them.)

- [ ] **Step 4: Template syntax check + commit**

```bash
python3 -c "from jinja2 import Environment, FileSystemLoader, select_autoescape; e = Environment(loader=FileSystemLoader('app/templates'), autoescape=select_autoescape(['html'])); e.get_template('intel_kills.html')"
git add app/templates/intel_kills.html
git commit -m "feat(kills): wire Most Valuable strip into /intel/kills page

Inserts the kf-top-strip-wrap above the filter bar, adds CSS for
.kf-top-* classes, and adds an isolated IIFE for the chevron toggle +
card-click → detail-slot wiring. The new JS is independent of the
live-feed IIFE so its bug surface stays contained.

Wraps up Feature A of
docs/superpowers/specs/2026-05-22-killfeed-most-valuable-7d-design.md."
```

- [ ] **Step 5: Deploy + browser smoke test**

```bash
ssh ijohnson@146.190.140.112 "/opt/vigilant/scripts/deploy.sh"
```

Expected: deploy completes, container healthy.

From a logged-in browser at `https://vigilant.thunderborn.dev/intel/kills`:

- **Initial render:** the page renders the existing `KILL FEED` header → after ~100ms the Most Valuable strip pops in above the filter rows (htmx lazy-load).
- **Cards populated:** Structures row has up to 6 ship/structure cards with image, type name, ISK (e.g. `33.44B`), system name, corp name. Same for Ships row.
- **Chevron collapse:** Click the `▾ MOST VALUABLE · LAST 7 DAYS` button → the two card rows collapse, chevron rotates to `▸`. Refresh the page → strip stays collapsed (localStorage). Click again → expands, persists.
- **Card click — open:** Click a card → the kill-detail panel slides down underneath the strip with the same look as the feed-row detail panel. Card gets a blue highlight border.
- **Card click — toggle:** Re-click the same card → panel slides up, card returns to normal style.
- **Card click — swap:** Click card A, then card B → panel content swaps to kill B, card B highlighted, card A no longer highlighted.
- **Live feed unaffected:** Scroll down to the live feed below. Click a feed-row → its detail expands inline. Both the strip's open detail and a feed-row's open detail can coexist on the page without conflict.
- **Browser console:** open devtools console, confirm no JS errors after loading the page or interacting with the strip.

If anything misbehaves, check `docker logs --tail 80 vigilant-app-1` for backend errors and the browser console for frontend errors. The most likely failure modes are: (a) wrong selector in `bindTopCards` (verify `#kf-top-strip-wrap .kf-top-card` matches your inserted markup), (b) the chevron toggle not finding `kf-top-body` (verify the partial template renders with the exact IDs in the steps above).

---

### Task 3: Post-deploy perf verification + optional index

**Goal:** Confirm the strip query is index-served and warm-path latency is within budget. Add the `(killmail_time, total_value)` index only if EXPLAIN shows a full table scan.

**Files:**
- Modify (conditional): production database via `docker exec` — `CREATE INDEX IF NOT EXISTS ix_killmails_time_value`
- Modify (conditional): `app/db/models.py` — add the same `Index()` to the `Killmail.__table_args__` so future re-deploys keep the index in source-of-truth.

**Acceptance Criteria:**
- [ ] EXPLAIN QUERY PLAN on each of the two top-N SELECTs shows index usage (either `USING INDEX ix_killmail_time` for the time predicate path or a new `ix_killmails_time_value` if added), not `SCAN TABLE killmails`.
- [ ] Warm-path `/intel/kills/top` latency in `/data/logs/perf.log` is < 50ms median.
- [ ] Cold-path `/intel/kills/top` latency is < 500ms median.
- [ ] If `sde_types.category_id IS NULL` count > 0 on the live DB, document the count (separate follow-up — out of scope for this task).

**Verify:**
1. EXPLAIN output captured below.
2. perf.log snapshot showing 10+ samples post-deploy.

**Steps:**

- [ ] **Step 1: Verify `sde_types.category_id` is populated**

```bash
ssh ijohnson@146.190.140.112 "docker exec vigilant-app-1 sqlite3 /data/vigilant.db 'SELECT COUNT(*) AS null_cat FROM sde_types WHERE category_id IS NULL;'"
```

Expected: `0`. If non-zero, the SDE loader's backfill step (`app/sde/loader.py:524`) did not run successfully — the Structures and Ships queries would return empty. Flag as a separate issue and run the backfill manually:

```bash
ssh ijohnson@146.190.140.112 "docker exec vigilant-app-1 sqlite3 /data/vigilant.db 'UPDATE sde_types SET category_id = (SELECT category_id FROM sde_groups WHERE sde_groups.group_id = sde_types.group_id) WHERE category_id IS NULL;'"
```

Re-run the COUNT query → should now be `0`.

- [ ] **Step 2: EXPLAIN the top-N queries**

```bash
ssh ijohnson@146.190.140.112 "docker exec vigilant-app-1 sqlite3 /data/vigilant.db \"EXPLAIN QUERY PLAN SELECT k.* FROM killmails k JOIN sde_types t ON t.type_id = k.victim_ship_type_id WHERE t.category_id = 65 AND k.killmail_time >= datetime('now', '-7 days') AND k.total_value IS NOT NULL ORDER BY k.total_value DESC LIMIT 6;\""
```

Repeat for `category_id = 6`.

Expected: at least one of the table accesses uses `USING INDEX`. Acceptable shapes include any of:
- `SEARCH k USING INDEX ix_killmail_time (killmail_time>?)` — fast.
- `SEARCH t USING INDEX <pk>` after a join — fast.
- Full scan of `killmails` with no index → **slow**; proceed to Step 3 to add the index.

- [ ] **Step 3: Add the index if needed**

If Step 2 showed a full `SCAN TABLE killmails`:

```bash
ssh ijohnson@146.190.140.112 "docker exec vigilant-app-1 sqlite3 /data/vigilant.db 'CREATE INDEX IF NOT EXISTS ix_killmails_time_value ON killmails(killmail_time, total_value);'"
```

Re-run Step 2 → should now show `SEARCH k USING INDEX ix_killmails_time_value`.

Then add the matching declaration to `app/db/models.py` so future schema re-creates carry the index. Find the `Killmail` class around line 663 — its `__table_args__` currently has one Index declaration:

```python
    __table_args__ = (
        Index("ix_killmail_system_time", "solar_system_id", "killmail_time"),
    )
```

Change to:

```python
    __table_args__ = (
        Index("ix_killmail_system_time", "solar_system_id", "killmail_time"),
        Index("ix_killmails_time_value", "killmail_time", "total_value"),
    )
```

Syntax-check and commit:

```bash
python3 -c "import ast; ast.parse(open('app/db/models.py').read())"
git add app/db/models.py
git commit -m "perf(kills): index killmails(killmail_time, total_value)

Added on the VPS via CREATE INDEX IF NOT EXISTS after EXPLAIN QUERY PLAN
showed a full table scan for the /intel/kills/top last-7-days top-N
query. Mirrored in models.py so future schema rebuilds carry it.

create_all skips indexes on existing tables — the post-deploy index hook
(_create_missing_indexes in models.py) handles this on next boot."
```

Then deploy so the index declaration is in the running image (`deploy.sh`).

(If Step 2 already showed index usage, skip this step entirely — no commit required.)

- [ ] **Step 4: Measure cold + warm latencies**

Briefly enable perf logging if it isn't already on (it's a config flag):

```bash
ssh ijohnson@146.190.140.112 "docker exec vigilant-app-1 env | grep VIGILANT_PERF_LOG"
```

If empty, set it (per-deploy env tweak — keep the rest of `docker-compose.yml` unchanged). Otherwise skip.

Trigger a cold-cache request — wait 6 minutes for the cache to expire OR restart the container to clear the in-memory `_top_cache`:

```bash
ssh ijohnson@146.190.140.112 "docker compose -f /opt/vigilant/docker-compose.yml restart app"
```

Hit the endpoint from your logged-in browser. Then check the perf log:

```bash
ssh ijohnson@146.190.140.112 "tail -n 20 /data/logs/perf.log | grep '/intel/kills/top'"
```

Expected:
- First hit (cold): `< 500ms`.
- Subsequent hits within the 5-min TTL (warm): `< 50ms`.

If cold-path exceeds 500ms, re-check Step 2 EXPLAIN output and verify the index was applied. If warm-path exceeds 50ms, the SWR cache isn't hitting — check that `_top_cache` is being populated (add a log line and re-deploy).

- [ ] **Step 5: Final acceptance walk-through**

Open `https://vigilant.thunderborn.dev/intel/kills` in a fresh incognito (so cache cookies/localStorage are clean). Verify the full UX:

1. Page renders → strip pops in within ~500ms.
2. Six structure cards + six ship cards visible.
3. Click any card → detail panel opens beneath the strip.
4. Toggle the chevron → strip collapses; refresh; collapse persists.
5. Live feed below continues polling every 15s (visible `kf-status` counter updates).
6. No console errors.

If all six pass, Feature A is complete. Commit any pending changes:

```bash
git status        # confirm nothing else uncommitted from this work
```

Then add a session memory if any non-obvious takeaway emerged (e.g. the EXPLAIN result, an unexpected category_id NULL count, a UX tweak you want recorded for Feature B). Otherwise nothing more to record.

---

## Self-Review Notes

- **Spec coverage:** Every section of the spec maps to a task: Strip placement / cards / collapse + click → Tasks 1 (partial template) + 2 (page integration). Backend route + SWR cache → Task 1. Index check → Task 3. Name resolution + system band tint → Task 1's `_compute_top_context`. Non-goals are honored (no Sponsored row, no per-region scope, no live updates).
- **No placeholders:** All code blocks are complete; all commands include expected output; all file paths are absolute or rooted at the repo.
- **Type / name consistency:** `_top_cache`, `_top_revalidating`, `_TOP_TTL`, `_compute_top_context`, `_refresh_top_background`, `_fmt_top_isk`, `_band` — used consistently across Task 1 steps. CSS class names `.kf-top-card`, `.kf-top-grid`, `#kf-top-body`, `#kf-top-toggle`, `#kf-top-detail-slot`, `#kf-top-strip-wrap` — used consistently in the partial (Step 6 of Task 1) and the page/JS (Steps 1-3 of Task 2).
