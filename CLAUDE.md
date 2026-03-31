# Vigilant VPS

## Project Overview section
This project (Vigilant) is an EVE Online companion dashboard deployed on a DigitalOcean VPS via SSH. Always assume the app runs remotely — never treat it as a local project unless explicitly told otherwise.

## Access
- **VPS**: `ssh ijohnson@146.190.140.112`
- **Code**: `/opt/vigilant/` on VPS
- **GitHub**: `Thor6677/Vigilant`
- **Live URL**: `https://vigilant.thunderborn.dev`

## Deploy
Code changes are baked into the Docker image — **always rebuild**:
```
cd /opt/vigilant && docker compose down && docker compose up -d --build
```
`docker compose restart` alone does NOT apply code or template changes.

### Pre-Deploy Checklist (mandatory before every VPS deploy)
Run these checks autonomously before touching production. If any check fails, **stop and report** — do not deploy.

1. **Tests** — Run all tests locally and confirm green
2. **Docker build** — `docker compose build` locally and verify it succeeds
3. **SSH/auth safety** — Review changed files for anything that modifies SSH config, auth, firewall rules, or service configs — flag these for explicit user approval
4. **Nginx validation** — After deploying, run `docker exec vigilant-nginx-1 nginx -t` to validate config
5. **Database safety** — Check that any model/schema changes won't fail on existing data (new columns need defaults or migrations)
6. **Startup verification** — After deploy, check `docker logs vigilant-app-1` for errors and confirm the app is serving requests

## SDE Reload
After adding new SDE tables/columns: (1) rebuild & deploy, (2) `docker exec` to create tables via `Base.metadata.create_all`, (3) delete `sde_meta` `last_updated` row, (4) restart to trigger reimport.

## Tech Stack
- **Backend**: FastAPI + Python (async), SQLite database, scipy for LP solver
- **Templates**: Jinja2 (in `app/templates/`, partials in `app/templates/partials/`)
- **Dynamic loading**: htmx (loaded globally in `base.html`)
- **Deployment**: Docker + Nginx, Cloudflare proxy in front
- **State persistence**: localStorage for manufacturing/compression calculator settings
- **Security**: Fernet-encrypted ESI tokens at rest (derived from secret_key via PBKDF2)
- **Caching**: ESI response cache with per-endpoint TTLs (`app/db/cache.py`)

## Key Gotchas

## Debugging section
When debugging production issues, always start by checking VPS logs (`ssh` into the server and inspect container/app logs) before guessing at causes like browser cache or adding debug logging. Identify the root cause first.

### Jinja2 dict access
Use `dict['key']` not `dict.key` for keys that match Python dict methods (`items`, `keys`, `values`, etc.).

### Dynamic content loading
Prefer **htmx** (`hx-get`, `hx-trigger="load"`) over JavaScript `fetch()` for loading content dynamically.

### Jinja2 script blocks
Scripts must be **inside** `{% block content %}` before `{% endblock %}`. Content after `{% endblock %}` is discarded.

### htmx partial script override
htmx-loaded partials can redefine JS functions from the parent page. Put canonical functions in the partial (loads last) or use `window.fn = window.fn || function(){}`.

### SQLAlchemy detached instances
When passing Character objects to templates after async ESI calls, extract fields into a plain dict first to avoid lazy-load errors on detached SQLAlchemy models.

### Compressed ore portion sizes
New compressed ores (62xxx) have `portionSize=100`. typeMaterials quantities are per batch, not per unit. LP solver works in batches.

### Eve skill system (post-Equinox)
Ore processing uses tier-based skills: Simple (60377), Coherent (60378), Variegated (60379), Complex (60380), Abyssal (60381), Mercoxit (12189). Not the old per-ore skills.

## Architecture Notes

### Page Structure
- **Dashboard** (`/dashboard`) — Character cards with grouping/sort/drag-and-drop, wealth breakdown, industry/orders/clones/PI/mail/notifications/contracts/zkill, EVE server time clock
- **Character Detail** (`/character/{id}`) — Wallet chart, journal, skills, implants, clones, assets, mail, notifications
- **Sub-pages**: journal, skills (remap optimizer), fittings (EFT export), blueprints, mining
- **Corporation pages**: overview, journal (7 divisions), blueprints, mining
- **Industry** (`/industry`) — Manufacturing calculator with nested build/buy, build times, shopping list
- **Compression** (`/industry/compression`) — LP solver for optimal ore purchasing
- **Mining Ledger** (`/industry/mining`) — Unified cross-character/corp mining ledger with stacked ore chart
- **Intel** — Gate Check (`/intel/gatecheck`): route safety, gatecamp finder, war targets via zKillboard; D-Scan (`/intel/dscan`): paste parser with ship categorization and shareable results
- **Static assets**: Logo at `static/logo.png`, favicon at `static/favicon.ico`

### Key Models & Tables
- `User` — Player account, identified by main EVE character
- `Character` — EVE character with encrypted tokens, user_id, is_main, account_group
- `CharacterDashboardCache` — 12 JSON fields synced in background
- `CharacterAssetCache` — Pre-resolved asset data
- `MiningLedgerEntry` — Persistent mining ledger (survives ESI's 30-day window)
- `DScanResult` — Shareable D-Scan parse results
- `WalletSnapshot` — Historical wallet balance for charting
- `ESIRateLimitEvent` — Rate limit tracking (429/420 events)
- `SDEBlueprintMaterial` — Manufacturing materials (activity_id=1)
- `SDEBlueprintInfo` — Manufacturing time + product mapping
- `SDETypeMaterial` — Reprocessing yields (46K rows)
- `SDECompressible` — Raw → compressed ore mapping (206 rows)
- `SDEType` — Item types with volume, portion_size
- `SDESystem/Station/Jump/Region/Constellation` — Navigation graph for route planning

### Industry Modules
- `app/industry/compression.py` — LP solver (scipy.optimize.linprog), yield calculation, ore skill mapping
- `app/routes/industry.py` — Manufacturing + compression routes
- Manufacturing: `_calc_material()`, `_calc_time()`, nested component expansion
- Compression: `compute_yield()`, `solve_compression()` with ISK/Volume/Waste modes

## Server Administration section
Before making SSH/security changes on remote servers: 1) Always ensure a non-root user with sudo exists before disabling root login. 2) Verify shell syntax (bash vs csh/tcsh on FreeBSD/TrueNAS). 3) Never lock out the access path you're currently using.

## Workflow section
Always commit and push code at the end of a session. Before finishing, ask: 'Should I commit and push these changes?'
