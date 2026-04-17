# Vigilant VPS

EVE Online companion dashboard deployed on a DigitalOcean VPS via SSH. **Always assume the app runs remotely** — never treat it as a local project unless explicitly told otherwise.

Feature descriptions and full architecture live in @README.md. Backend stack in @requirements.txt, frontend stack in @frontend/package.json.

## Access
- **VPS**: `ssh ijohnson@146.190.140.112`
- **Code**: `/opt/vigilant/` on VPS
- **GitHub**: `Thor6677/Vigilant`
- **Live URL**: `https://vigilant.thunderborn.dev`

## Deploy
Code changes are baked into the Docker image — **always rebuild**. Prefer the deploy script (it tags the running image as `:prev` before rebuilding so a bad deploy can be rolled back instantly):
```
ssh ijohnson@146.190.140.112 "/opt/vigilant/scripts/deploy.sh"
```
Manual fallback (equivalent, no rollback tag):
```
cd /opt/vigilant && docker compose down && docker compose up -d --build
```
`docker compose restart` alone does NOT apply code or template changes.

### Pre-Deploy Checklist (mandatory before every VPS deploy)
If any check fails, **stop and report** — do not deploy.

1. **Syntax check** — `python3 -c "import ast; ast.parse(open('file').read())"` on all modified `.py` files
2. **SSH/auth safety** — Review changed files for anything touching SSH config, auth, firewall, or service configs — flag for explicit user approval
3. **Database safety** — New columns need defaults or `ALTER TABLE` in `main.py` migration block. New tables auto-create via `Base.metadata.create_all`
4. **Nginx validation** (after deploy) — `docker exec vigilant-nginx-1 nginx -t`
5. **Startup verification** (after deploy) — `docker logs vigilant-app-1` for errors, confirm app is serving

## Rollback
Two paths, pick based on what broke.

**Code is bad → use `git revert` (the canonical rollback).** This is the right choice 99% of the time. It preserves history, works under any branch settings, and keeps the code and image in sync.
```
# on laptop
git revert <bad-commit>                     # creates a new commit that undoes the change
git push origin main
# on VPS
/opt/vigilant/scripts/deploy.sh
```

**Image is broken and service needs to come back NOW → use `rollback.sh`.** Instantly swaps the `:prev` image back in without rebuilding (~5s). Only use this when the runtime is broken and you can't wait for a rebuild. The repo still has the bad commit after this, so you MUST follow up with the `git revert` path above, otherwise the next rebuild re-introduces the bug.
```
ssh ijohnson@146.190.140.112 "/opt/vigilant/scripts/rollback.sh"
# then, on laptop, do the git revert path above to re-sync code
```

The rollback script tags the broken image as `vigilant-app:broken` for later inspection. `:prev` is refreshed on every `deploy.sh` run, so you always have one-level-back protection; it does NOT accumulate a history.

## SDE Reload
After adding new SDE tables/columns: (1) rebuild & deploy, (2) `docker exec` to create tables via `Base.metadata.create_all`, (3) delete `sde_meta` `last_updated` row, (4) restart to trigger reimport.

## Debugging
When debugging production issues, always start by checking VPS logs (`ssh` into the server and inspect container/app logs) before guessing at causes like browser cache or adding debug logging. Identify the root cause first.

## Key Gotchas

### Jinja2 dict access
Use `dict['key']` not `dict.key` for keys that match Python dict methods (`items`, `keys`, `values`, etc.). Dot notation returns the method object, not the value.

### Dynamic content loading
Prefer **htmx** (`hx-get`, `hx-trigger="load"`) over JavaScript `fetch()`. htmx is already initialized globally in `base.html`; fetch inside IIFEs can silently fail and is painful to debug.

### Jinja2 script blocks
Scripts must be **inside** `{% block content %}` before `{% endblock %}`. Content after `{% endblock %}` is discarded. **Never duplicate script blocks** in both head and content blocks.

### htmx partial script override
htmx-loaded partials can redefine JS functions from the parent page. Put canonical functions in the partial (loads last) or use `window.fn = window.fn || function(){}`.

### SQLAlchemy detached instances
When passing Character objects to templates after async ESI calls, extract fields into a plain dict first to avoid lazy-load errors on detached models.

### Async session safety
Never share a single `AsyncSession` across `asyncio.gather` coroutines that do writes. Each concurrent coroutine needs its own `AsyncSessionLocal()` session. Use `get_client_safe()` from `app/esi/client.py` for concurrent token refresh.

### Corp ESI calls need fallback
Always use `_try_api_call_with_fallback()` for corp ESI endpoints — a single character may lack the in-game Director role and get 403. Cycle through all characters with the scope.

### Corp asset office mapping
Corp hangar items have `location_id` pointing to an office container, NOT the structure directly. Use `_build_office_to_structure_map()` from `app/routes/corporations.py`.

### Compressed ore portion sizes
New compressed ores (62xxx) have `portionSize=100`. `typeMaterials` quantities are per batch, not per unit. LP solver works in batches.

### Eve skill system (post-Equinox)
Ore processing uses tier-based skills: Simple (60377), Coherent (60378), Variegated (60379), Complex (60380), Abyssal (60381), Mercoxit (12189). Not the old per-ore skills.

### Alert banner dismiss pattern
Site-wide alert banners (structure/inventory/contract/timer) start with `display:none`. Inline script shows non-dismissed ones. A **global `htmx:afterSwap` handler in `base.html`** re-applies dismiss state after every swap — without it, dismissed banners reappear on navigation. Never invert to visible-by-default.

### ESI bulk-fetch pattern (avoiding 429s)
When iterating N items to call ESI per-item (e.g. contract items for 200+ contracts):
1. Phase 1: `cache_get()` check all items, build `uncached` list
2. Phase 2: Semaphore of 3 + batches of 10 + `await asyncio.sleep(1)` between batches
3. `cache_set()` after each fetch. Add TTL match in `app/db/cache.py:_ttl_for_path()`.

Don't use `asyncio.gather(*[fetch(x) for x in items])` for N > 20 to the same endpoint group.

### UTC datetime inputs
Avoid HTML `<input type="datetime-local">` for UTC fields — it forces browser timezone display/conversion. Use plain `<input type="text">` with `YYYY-MM-DD HH:MM` placeholder and `datetime.strptime(dt_str, "%Y-%m-%d %H:%M")` server-side. See `structure_timers.html` and `structure_timers.py`.

## Workflow
Always commit and push code at the end of a session. Before finishing, ask: "Should I commit and push these changes?"

## Session Log

### 2026-04-17 — Wormhole Reference Tools (anoik.is-style)
**Built**: Complete wormhole reference toolset under Intel dropdown:
- `/wormholes` — System database with anoik.is-style toggle filters (class, statics, planet types, effects, Perfect PI)
- `/wormholes/system/{name}` — System detail with connections, SVG orbital diagram (Full/D-scan zoom), celestials (planet types, AU distances, moon counts, star type), zKillboard 30-day kill heatmap with recency opacity + recent kills feed
- `/wormholes/types` — Connection matrix (From×To grid) with clickable type detail (mass, lifetime, respawn)
- `/wormholes/effects` — All 6 wormhole effects with modifier tables per class (C1-C6)
- Perfect PI filter using PI constants (`app/pi/constants.py`) — checks all 15 P0 raw materials are producible
- Wanderer-matching class colors sourced from wanderer-industries/wanderer SCSS

**Fixed**: PI calculator 422 on empty max_chars (auto mode)

**New SDE data**: SDEWormholeClass, SDEWormholeType, SDEMoon, SDEStar models; expanded planet import to all types with distance_au; wormhole type dogma attributes (1382=time, 1383=mass); star names resolved during import; moon parent via orbitID

**Data**: `app/data/wormholes.json` — 2,568 system statics + 1,038 system effects (sourced from WHDBX), connection matrix, wormhole metadata, Wanderer class colors

**Files**: `app/routes/wormholes.py`, `app/sde/lookup.py`, `app/sde/loader.py`, `app/db/sde_models.py`, `app/data/wormholes.json`, `app/main.py`, `app/routes/pi.py`, `app/templates/base.html`, 4 new page templates, 3 new partials, `.claude/commands/wrap.md`

**Commits**: 27 commits, ~20 deploys, multiple SDE reimports
**State**: All features deployed and live at vigilant.thunderborn.dev. SDE data current.
**Next**: The `wormholes.json` system_statics data covers ~2,568 of ~2,600 systems — a few edge cases (shattered, drifter) may be missing. The wormhole type detail page could show which systems have that type as a static. The system diagram planet placement uses golden angle (cosmetic, not actual orbital positions).
