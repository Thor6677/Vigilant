# Vigilant

<p align="center">
  <img src="static/logo.png" alt="Vigilant" width="180">
</p>

An EVE Online companion dashboard that gives you a unified view of all your characters — wallet, skills, industry, intel, a full interactive star map, and much more, all in one place.

![EVE Online](https://img.shields.io/badge/EVE%20Online-ESI-blue)
![Python](https://img.shields.io/badge/Python-3.12%2B-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115-green)
![React](https://img.shields.io/badge/React-TypeScript-61dafb)
![Pixi.js](https://img.shields.io/badge/Pixi.js-v8-e72264)

---

## Features

### Dashboard (`/dashboard`)
- **Multi-character overview** — character cards with portrait, corp/alliance, online status, wallet, location, current ship, skill training with progress bars, and sync status
- **Account grouping** — organize alts into named account groups with drag-and-drop reordering
- **Sort modes** — Grouped (custom), Name, Corp, Training, Queue End
- **Wealth breakdown** — per-character wallet bars with proportion visualization
- **Automatic background sync** — scheduler refreshes 12 ESI data fields per character on their cache timers (30s-1h); page loads never trigger ESI calls
- **Aggregated panels** — industry jobs, market orders, jump clones, planetary industry, mail, notifications, contracts, and zKillboard kills all on one page
- **Alert banners** — persistent banners for structure attacks, fuel alerts, sovereignty events, and critical inventory thresholds

### Star Map (`/map`)
- **Interactive 2D WebGL map** — ~5,485 K-space systems rendered with Pixi.js v8, ~6,984 stargate edges, pan/zoom/pinch with pixi-viewport
- **Data overlays** — Security status, Jumps, Ship/Pod/NPC Kills, Sovereignty (with alliance names and logos), Faction Warfare, Incursions — bottom bar selector with color legends
- **System info panel** — security, region, constellation, NPC station count, service badges (Clone/Manufacturing/Lab/Market/Refinery/Repair/Reprocessing/Jump Clone), sovereignty holder, kill/jump stats, DOTLAN and zKillboard links
- **Gate routing** — shortest path via Graphology with preferences (shortest/highsec/lowsec/nullsec)
- **Jump drive planner** — capital ship jump calculator with 8 ship classes, JDC/JFC skill levels, route preferences (prefer NPC station, prefer highsec gate), fatigue and fuel calculator, editable midpoints, alternative system lists, range viewer
- **Search** — find systems, regions, constellations, and services (type "cloning", "factory", "jc" etc. to find nearest service to a character)
- **Grouping modes** — System/Constellation/Region with expand-in-place
- **Character locations** — pulsing gold markers at character positions with "Locate Me" button
- **Live ESI data** — background polling for kills, jumps, sovereignty, faction warfare, and incursions with ETag caching

### Skill Plans (`/skill-plans`)
- **Create and edit skill plans** — add skills from search, ship requirements, mastery levels, or fittings
- **Gap analysis** — per-character skill gap with training time estimates
- **Import/Export** — import from EVE text format, export skill plans, share via public link
- **Drag-and-drop reorder** — sort by name, optimal training order, or level

### Ship Mastery (`/skill-plans/ship/{id}`)
- **Full prerequisite tree** — every skill required to fly a ship, expanded recursively
- **Mastery levels I-V** — CCP's official mastery certificates with progress tracking per character

### Intel

#### Gate Check (`/intel/gatecheck`)
- **Route safety analysis** — paste or search a route to check for gatecamps and danger
- **Gatecamp finder** — identifies likely gatecamp systems using zKillboard data
- **War target detection** — highlights systems with active war targets along your route

#### D-Scan (`/intel/dscan`)
- **Paste parser** — paste your directional scan results from the EVE client
- **Ship categorization** — groups results by ship class and type with counts
- **Shareable results** — generate a link to share D-Scan results with others

### Character Detail (`/character/{id}`)
- **Wallet history chart** — interactive Chart.js graph with time ranges (1d/5d/1w/1m/6m/1y)
- **Wallet journal** — last 20 entries inline + "Full Journal" link to dedicated page
- **Skills** — trained skills with remap optimizer, what-if simulator, per-skill comparison
- **Mail reader** — mail list with click-to-read body and sender name resolution
- **Notification feed** — parsed notification types with human-readable labels and summaries
- **Implants & jump clones** — attribute enhancers sorted by slot, hardwirings, per-clone implant sets
- **Assets** — background-synced asset browser grouped by location
- **Corp history** — full employment timeline

### Ship Fitting Tool (`/tools/fitting`)
- **Full fitting builder** — three-column layout with module browser (market-group tree), slot layout (high/mid/low/rig/subsystem/drone), and live stats
- **Dogma-accurate engine** — DPS, EHP, capacitor simulation, offense/defense/navigation/targeting/drones, stacking penalties, module-to-module bonuses (Bastion/Siege), Triglavian spool-up, T3C subsystems with per-level scaling
- **Character-accurate stats** — dropdown picks any of your characters; DPS/EHP/CPU/PG recompute against that character's actual trained skills (falls back to All V when no character selected)
- **Skill-requirement warnings** — ⚠ pip + red row highlight on any ship/module/drone the selected character can't use; hover tooltip lists missing skills with need/have levels
- **Module overheating** — toggle per module with correct per-type overload bonuses
- **Module info popup** — click any fitted module, drone, or search result for icon, description, and formatted dogma attributes
- **Warp speed** — AU/s in the Navigation panel, Hyperspatial rigs apply correctly
- **HP ↔ EHP and VAL ↔ % toggles** — flip Defense between raw HP and EHP-adjusted; flip Fitting Resources between absolute and percent used
- **EFT import/export** — paste from EVE or Pyfa with aggressive Unicode normalization; one-click export to clipboard
- **Import from character** — pulls your in-game saved fittings; Load to builder or Import to a saved folder

### Saved Fittings (`/tools/fitting/saved`)
- **Searchable, sortable table** — ship, fitting name, folder, DPS, estimated cost (live from `/markets/prices/`)
- **Nested folders** — create, rename, move, delete; folder membership persists per fit
- **Click to load** — any row opens the builder with that fit restored, ready to edit or compare

### Fitting Viewer (`/character/{id}/fittings`)
- **Slot-organized display** — High/Mid/Low/Rig/Subsystem/Drone/Cargo with ship slot counts
- **Ship render images** — grouped by ship type
- **EFT export** — "Copy EFT" button for pasting into EVE client or Pyfa

### Blueprint Library (`/character/{id}/blueprints`)
- **BPO/BPC display** — ME/TE research levels, remaining runs for copies
- **Filters** — All, BPO Only, BPC Only, Unresearched; group by Type or Location
- **Stats** — total count, originals, copies, researched, fully maxed (ME10/TE20)

### Manufacturing Calculator (`/industry`)
- **Blueprint search** — live search with full modifier support (ME, TE, structure, rig, security)
- **Nested build/buy** — click "Build" on any component to see its sub-BOM; recursive for sub-components
- **Build time estimates** — parallel build (components simultaneous + final assembly), sequential build, per-component times
- **Shopping list** — aggregated materials with multibuy-compatible copy
- **Send to Compressor** — one-click transfer of mineral requirements to the compression calculator
- **Persistent state** — settings saved in localStorage

### Compression Calculator (`/industry/compression`)
- **LP solver** — scipy linear programming finds mathematically optimal compressed ore mix
- **Three optimization modes** — Lowest ISK, Lowest Volume, Lowest Waste
- **Character skill integration** — auto-fetches reprocessing skills
- **Full reprocessing yield calculation** — structure, rig, security, implant modifiers
- **Trade hub selection** — Jita, Amarr, Dodixie, Hek, Rens

### Mining Ledger (`/industry/mining-ledger`)
- **Unified cross-character/corp mining** — aggregated view across all characters and corps
- **Stacked ore chart** — visual breakdown by ore type over time
- **Date range filters** — 7d, 30d, 90d, 6m, 1y
- **Per-character/corp views** also available at `/character/{id}/mining` and `/corporations/{id}/mining`

### Industry Jobs (`/industry/jobs`)
- **Combined active-jobs view** across every owned character and every corp where at least one character has the corp-jobs scope (Director fallback cycles through characters on 403)
- **Dropdown filters** — per-character, per-corp, per-activity chips, and source-kind toggle (all / characters only / corps only)
- **Structure name resolution** — shared `StructureNameCache` with proactive corp-structures prefetch so player citadels show up by name
- **Include-completed toggle** — widen the view to finished jobs when doing post-mortems
- **NPC corps surfaced honestly** — skipped automatically (their endpoint always 403s) but counted in the subtitle so the number stays explainable

### Structure Timers (`/structure-timers`)
- **Shared timer board** — manual entry with countdown or absolute UTC time
- **ESI auto-detection** — automatically detects structure reinforcement from ESI
- **Live countdown** — real-time JavaScript countdown timers
- **ACL groups** — control visibility by corporation, alliance, or individual character
- **Role-based permissions** — edit and delete access based on user roles

### Corporation Features
- **Corp overview** — wallet divisions, industry jobs, market orders, structures (fuel/reinforcement status), contracts, member list
- **Corp wallet journal** — 7 divisions with full filtering
- **Corp blueprints** — ME/TE, location, BPO/BPC filters
- **Corp mining** — aggregated across all characters with mining scope
- **Inventory tracker** — monitor corp hangar items with configurable alert thresholds

### Notifications & Alerts
- **Browser notifications** — bell icon with dropdown for skill completions, industry jobs, PI, mail, structure alerts, and inventory alerts
- **Structure alert banners** — persistent banners for structure attacks, fuel, sovereignty events, and moonmining. Deduped across characters
- **Inventory alert banners** — critical corp inventory thresholds shown as persistent banners
- **Granular muting** — per-type notification muting (structure attacks, fuel, sov, moonmining, POCO independently)

### Admin Panel (`/admin`)
- **System health** — scheduler status, DB stats, ESI health monitoring
- **User management** — view and manage users with role assignment (Admin/Manager/User)
- **Character management** — view all registered characters
- **SDE management** — trigger Static Data Export reimports
- **Audit log** — logins, sync errors, admin actions
- **Registration allowlist** — restrict registration by character, corporation, or alliance

### Other Features
- **ESI rate limit monitoring** — real-time dashboard with request activity chart and per-group tracking
- **Sync diagnostics** — per-field warnings, stale data indicators, manual resync buttons
- **Server status** — Tranquility online/offline indicator with player count and EVE time (UTC) clock
- **Cross-character asset search** — search assets across all your characters from `/assets`

---

## Quick Start (Local)

### Prerequisites

- **Python 3.12+** — [python.org](https://www.python.org/downloads/)
- **Node.js 22+** — [nodejs.org](https://nodejs.org/) (needed to build the star map frontend)
- **An EVE Online developer application** — [create one here](https://developers.eveonline.com/)
  - Set the callback URL to `http://localhost:8000/auth/callback`
  - You will need the **Client ID** and **Client Secret**

### Installation

```bash
# Clone the repository
git clone https://github.com/Thor6677/Vigilant.git
cd Vigilant

# Run the startup script
./start.sh
```

The `start.sh` script will:

1. Create a `.env` file from `.env.example` if one doesn't exist
2. Auto-generate a secure `SECRET_KEY`
3. Prompt you for your EVE SSO Client ID and Secret
4. Create a Python virtual environment and install dependencies
5. Launch Vigilant in the background
6. Wait for the app to confirm it's listening

Vigilant will be available at **http://localhost:8000** once startup is complete.

> **Note:** The local startup script runs the Python backend directly. The star map (`/map`) requires the React frontend to be built separately. To build it locally: `cd frontend && npm ci && npm run build`. The Docker deployment handles this automatically.

### View Logs

```bash
tail -f vigilant.log
```

### Stop

```bash
./stop.sh
```

---

## VPS / Server Deployment (Docker)

This section walks you through deploying Vigilant on a VPS (Virtual Private Server) from scratch. If you've never set up a server before, follow each step — everything you need is covered here.

### What You'll Need

- A **VPS** from any provider (DigitalOcean, Linode, Hetzner, Vultr, etc.) running **Ubuntu 24.04**
  - Minimum: 1 CPU, 1 GB RAM, 25 GB disk
  - Recommended: 2 CPU, 2 GB RAM (the star map build uses some memory)
- A **domain name** pointed at your VPS IP address (e.g., `vigilant.yourdomain.com`)
- An **EVE Online developer application** — [create one here](https://developers.eveonline.com/)
  - Set the callback URL to `https://vigilant.yourdomain.com/auth/callback` (use your actual domain)

### Step 1: Get a VPS

If you don't have a VPS yet:

1. Sign up at a provider like [DigitalOcean](https://www.digitalocean.com/), [Linode](https://www.linode.com/), [Hetzner](https://www.hetzner.com/cloud/), or [Vultr](https://www.vultr.com/)
2. Create an Ubuntu 24.04 server (called a "Droplet" on DigitalOcean, "Linode" on Linode, etc.)
3. Choose the cheapest plan that meets the minimum specs above
4. During creation, add your SSH key (or the provider will email you a root password)

**Connecting to your VPS:**
```bash
# From your local terminal (replace with your VPS IP)
ssh root@YOUR_VPS_IP
```

If you're on Windows, use [Windows Terminal](https://aka.ms/terminal) with the built-in SSH client, or [PuTTY](https://www.putty.org/).

### Step 2: Set Up the Server

Once connected to your VPS, run the included setup script:

```bash
# Download and run the setup script (as root)
curl -fsSL https://raw.githubusercontent.com/Thor6677/Vigilant/main/setup_vps.sh -o setup_vps.sh
chmod +x setup_vps.sh
./setup_vps.sh
```

This installs Docker, Docker Compose, git, and creates a `vigilant` system user with the app directory at `/opt/vigilant`.

### Step 3: Point Your Domain

Before getting an SSL certificate, your domain needs to point to your VPS:

1. Go to your domain registrar's DNS settings (Cloudflare, Namecheap, etc.)
2. Add an **A record**:
   - **Name**: `vigilant` (or whatever subdomain you want)
   - **Value**: Your VPS IP address
   - **TTL**: Auto or 300
3. Wait a few minutes for DNS to propagate. You can check with:
   ```bash
   dig vigilant.yourdomain.com
   ```

### Step 4: Stand up a reverse proxy

Vigilant's app container exposes port 8000 internally on a Docker network — it does NOT bind 80/443. You need a reverse proxy in front of it that terminates TLS and routes traffic to the container.

If you already have nginx/caddy/traefik on the host, point it at `vigilant-app-1:8000` on the `web` Docker bridge. If you don't, the simplest path is to copy `docs/nginx-sample.conf` from this repo into `/etc/nginx/conf.d/` (or use a small companion nginx container) and adjust the `server_name` + cert paths.

You'll need a TLS cert. Two common options:

- **Let's Encrypt** — `certbot certonly --standalone -d vigilant.yourdomain.com`, then point the proxy at `/etc/letsencrypt/live/vigilant.yourdomain.com/`.
- **Cloudflare Origin Certificate** (if you proxy through Cloudflare) — generate from **SSL/TLS** > **Origin Server**, save the cert + key wherever your proxy expects them.

Create the shared Docker network so the proxy and the app can talk:
```bash
docker network create web
```

### Step 5: Clone and Configure

```bash
# Switch to the vigilant user
su - vigilant

# Clone the repository
git clone https://github.com/Thor6677/Vigilant.git /opt/vigilant
cd /opt/vigilant

# Create your config file
cp .env.example .env
nano .env
```

Edit `.env` with your settings:
```bash
EVE_CLIENT_ID=your_eve_client_id
EVE_CLIENT_SECRET=your_eve_client_secret
EVE_CALLBACK_URL=https://vigilant.yourdomain.com/auth/callback
SECRET_KEY=generate_a_random_key_see_below
DEBUG=false
```

Generate a secure `SECRET_KEY`:
```bash
python3 -c "import secrets; print(secrets.token_urlsafe(64))"
```

### Step 6: Deploy

```bash
cd /opt/vigilant
docker compose up -d --build
```

The first build takes a few minutes (it installs Python dependencies and builds the React frontend). Subsequent builds are faster thanks to Docker's layer cache.

**Verify everything is working:**
```bash
# Check the app container is running
docker compose ps

# Check app logs for errors
docker compose logs app

# Hit the healthcheck through the proxy
curl https://vigilant.yourdomain.com/healthz
```

Visit `https://vigilant.yourdomain.com` — you should see the login page.

### Managing Your Deployment

```bash
# View live logs
docker compose logs -f

# View just app logs
docker compose logs -f app

# Restart (does NOT apply code changes)
docker compose restart

# Rebuild and redeploy (REQUIRED for code changes)
docker compose down && docker compose up -d --build

# Stop everything
docker compose down
```

> **Important:** `docker compose restart` does NOT apply code or template changes. Always use `docker compose down && docker compose up -d --build` after pulling new code.

### Updating to a New Version

```bash
cd /opt/vigilant
git pull origin main
docker compose down && docker compose up -d --build
```

The app automatically migrates the database schema on startup.

---

## EVE Online Developer Application Setup

1. Go to [developers.eveonline.com](https://developers.eveonline.com/)
2. Log in with your EVE Online account
3. Click **"Applications"** > **"Create New Application"**
4. Fill in the form:
   - **Application Name**: `Vigilant` (or any name you like)
   - **Description**: Personal EVE character dashboard
   - **Connection Type**: **Authentication & API Access**
   - **Permissions**: Select all scopes listed in [ESI Scopes](#esi-scopes) below
5. Set the **Callback URL**:
   - Local: `http://localhost:8000/auth/callback`
   - VPS: `https://vigilant.yourdomain.com/auth/callback`
6. Create the application and save your **Client ID** and **Client Secret**

---

## Configuration Reference

All settings are read from `.env`:

| Variable | Default | Description |
|---|---|---|
| `EVE_CLIENT_ID` | *(required)* | EVE SSO application client ID |
| `EVE_CLIENT_SECRET` | *(required)* | EVE SSO application client secret |
| `EVE_CALLBACK_URL` | `http://localhost:8000/auth/callback` | OAuth callback URL — must match your ESI app |
| `SECRET_KEY` | *(auto-generated locally)* | Session and token encryption key |
| `DATABASE_URL` | `sqlite+aiosqlite:///./vigilant.db` | Database path (Docker overrides to `/data/vigilant.db`) |
| `DEBUG` | `false` | Enables FastAPI docs at `/api/docs` and verbose logging |

---

## ESI Scopes

Vigilant requests the following ESI scopes when authenticating a character:

<details>
<summary>Click to expand full scope list</summary>

| Scope | Purpose |
|---|---|
| `esi-wallet.read_character_wallet.v1` | Wallet balance and journal |
| `esi-location.read_location.v1` | Current system location |
| `esi-location.read_ship_type.v1` | Current ship type |
| `esi-location.read_online.v1` | Character online status |
| `esi-assets.read_assets.v1` | Character assets |
| `esi-industry.read_character_jobs.v1` | Industry jobs |
| `esi-clones.read_clones.v1` | Clone locations |
| `esi-clones.read_implants.v1` | Implant data |
| `esi-markets.read_character_orders.v1` | Market orders |
| `esi-mail.read_mail.v1` | Mail headers and bodies |
| `esi-characters.read_notifications.v1` | In-game notifications |
| `esi-contracts.read_character_contracts.v1` | Contracts |
| `esi-planets.manage_planets.v1` | Planetary interaction data |
| `esi-skills.read_skillqueue.v1` | Skill queue |
| `esi-skills.read_skills.v1` | Trained skills and attributes |
| `esi-fittings.read_fittings.v1` | Saved ship fittings |
| `esi-characters.read_blueprints.v1` | Character blueprints |
| `esi-characters.read_corporation_roles.v1` | Corporation role check |
| `esi-industry.read_character_mining.v1` | Mining ledger |
| `esi-corporations.read_corporation_membership.v1` | Corp member list |
| `esi-wallet.read_corporation_wallets.v1` | Corp wallet divisions |
| `esi-industry.read_corporation_jobs.v1` | Corp industry jobs |
| `esi-markets.read_corporation_orders.v1` | Corp market orders |
| `esi-corporations.read_structures.v1` | Corp structures |
| `esi-contracts.read_corporation_contracts.v1` | Corp contracts |
| `esi-assets.read_corporation_assets.v1` | Corp assets |
| `esi-corporations.read_blueprints.v1` | Corp blueprints |
| `esi-industry.read_corporation_mining.v1` | Corp mining observers |

</details>

Character-level scopes are always requested. Corporation-level scopes are only usable if the character has the required in-game roles (e.g., Director). See [ESI documentation](https://esi.evetech.net/) for details.

---

## Pages

| Route | Description |
|---|---|
| `/dashboard` | Main dashboard with character cards, grouping, wealth, summary panels |
| `/map` | Interactive star map with overlays, routing, jump planner |
| `/skill-plans` | Skill plan manager with create/edit/share/gap analysis |
| `/skill-plans/ship/{id}` | Ship mastery viewer with prereq tree |
| `/intel/gatecheck` | Route safety checker with gatecamp and war target detection |
| `/intel/dscan` | D-Scan paste parser with ship categorization |
| `/structure-timers` | Shared structure timer board with ACL |
| `/industry` | Manufacturing calculator with nested build/buy |
| `/industry/compression` | Compression calculator with LP solver |
| `/industry/mining-ledger` | Unified cross-character/corp mining ledger |
| `/industry/jobs` | Combined active industry jobs across characters and corps |
| `/tools/fitting` | Ship fitting builder with Dogma engine and per-character skill scaling |
| `/tools/fitting/saved` | Saved fittings list with DPS/cost and folder tree |
| `/character/{id}` | Character detail — wallet, skills, mail, notifications, assets |
| `/character/{id}/journal` | Full wallet journal with category filtering |
| `/character/{id}/skills` | Skill remap optimizer and what-if simulator |
| `/character/{id}/fittings` | Ship fitting viewer with EFT export |
| `/character/{id}/blueprints` | Blueprint library with ME/TE and filters |
| `/character/{id}/mining` | Character mining ledger |
| `/assets` | Cross-character asset search |
| `/corporations/{id}` | Corp overview — wallet, structures, jobs, orders, members |
| `/corporations/{id}/journal` | Corp wallet journal (7 divisions) |
| `/corporations/{id}/blueprints` | Corp blueprint library |
| `/corporations/{id}/mining` | Corp mining ledger |
| `/status` | ESI rate limits, sync health, request activity |
| `/admin` | Admin panel — health, users, SDE, audit log (admin/manager only) |

---

## Security

### Authentication & Encryption

- **EVE Online SSO** — login handled entirely by CCP's official OAuth2. No passwords stored.
- **Token encryption** — ESI access and refresh tokens are encrypted at rest using Fernet (AES-128-CBC + HMAC-SHA256), with the key derived from `SECRET_KEY` via PBKDF2-SHA256 (100k iterations).
- **User isolation** — every database query is scoped to the authenticated user. One user cannot access another's data.
- **Session security** — signed cookies via `itsdangerous`, `HttpOnly`, `SameSite=Lax`, `Secure` in production, 30-day expiry.
- **State validation** — OAuth callbacks validate a CSRF state token to prevent redirect hijacking.

### Transport & Docker Hardening

- **HTTPS expected** — sample reverse-proxy config (`docs/nginx-sample.conf`) terminates TLS 1.2/1.3, redirects HTTP→HTTPS, sets HSTS (2 years), and adds `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy`, `Permissions-Policy`.
- **Security headers** — recommended set baked into the sample vhost; replicate in your own proxy config if you don't use it.
- **Container hardening** — read-only filesystem, `no-new-privileges`, `cap_drop: ALL` (with minimal `cap_add: [CHOWN, SETUID, SETGID]` consumed by the entrypoint to drop to a non-root `vigilant` uid before uvicorn starts), memory/CPU/PID limits

### First-User Admin Bootstrap (Accepted Risk)

Vigilant has no out-of-band admin provisioning step. Instead, on every app startup the bootstrap routine in `app/main.py` runs:

> If no user has `is_admin=true`, promote the user with the lowest `id` to admin.

In practice this means **the operator should sign up first after a fresh deploy** — that account becomes admin on the next app restart (or immediately, since the bootstrap runs at startup). Once an admin exists, the routine is a no-op forever.

**The risk:** between a fresh deploy and the operator's first signup, anyone who reaches the URL with a valid EVE Online character could sign up first and end up as admin. For a self-hosted, single-operator deployment behind Cloudflare with EVE SSO required, this attack window is small in practice — but it is real and is flagged by static analysis (sec-toolkit finding VVP-2026-013, CWE-269).

**Why this is accepted rather than fixed:** the operator controls deploy timing, the existing `/admin` flow lets admins promote/demote, and the `users` table only contains people who completed full EVE SSO with a real character — not anonymous signups. Replacing this with an env-var allowlist or bootstrap-window flag (paths B / C in the original ticket discussion) would add operational friction without meaningfully changing the threat model for this deployment shape.

**If you fork Vigilant for a deployment where this isn't acceptable** (multi-tenant, public-facing without admin pre-provisioning, etc.), replace the bootstrap block in `app/main.py` with an `ADMIN_EVE_IDS=12345,67890` env-var allowlist that only auto-promotes character IDs in the list, or a one-shot `ADMIN_BOOTSTRAP=true` env flag the operator sets for the first deploy and then unsets.

### Production Checklist

1. Use HTTPS with a valid TLS certificate
2. Generate a strong `SECRET_KEY` and never change it while the database has active users
3. Set `.env` permissions: `chmod 600 .env`
4. Never commit `.env` to git
5. Set `DEBUG=false` in production
6. Back up the SQLite database regularly (`/data/vigilant.db` in Docker)

### Data Privacy

- Your data is stored on your own server — not sent to external servers (except EVE's ESI and zKillboard for public kill data)
- No telemetry or analytics
- Fully open source and auditable

---

## Tech Stack

| Layer | Technology |
|---|---|
| **Backend** | FastAPI, SQLAlchemy (async), aiosqlite, Uvicorn, scipy (LP solver) |
| **Templates** | Jinja2, htmx, Tailwind CSS (built at image build), Chart.js |
| **Star Map** | React, TypeScript, Vite, Pixi.js v8 (WebGL), pixi-viewport, d3-quadtree, Graphology |
| **Data** | EVE ESI REST API, zKillboard API, EVE SDE (Static Data Export) |
| **Deployment** | Docker, Docker Compose; bring-your-own reverse proxy (sample nginx vhost in `docs/`) |

---

## FAQ

**Is my data safe?**
Yes. All data is stored on your own machine or server. ESI tokens are encrypted at rest. Use HTTPS and a strong `SECRET_KEY` in production.

**Can I use Vigilant with multiple characters?**
Yes. Click "Add Character" and authenticate through EVE SSO. You can add as many characters as you want and organize them into account groups.

**How often is data refreshed?**
The background sync runs every 60 seconds, respecting ESI cache timers: location (30s), wallet (2m), skills (5m), industry (2m), markets (5m), clones/implants (1h).

**Does this violate EVE Online's Terms of Service?**
No. Vigilant uses only official ESI endpoints. It doesn't automate gameplay or interact with the EVE client.

**How do I remove a character?**
Go to `/characters` and click the remove button next to the character.

**How do I access the admin panel?**
The first user to register is automatically an admin. Admins can promote other users to Manager or Admin roles from `/admin`.

---

## Troubleshooting

**App won't start:**
```bash
# Local
tail -30 vigilant.log

# Docker
docker compose logs app
```
Common causes: Python too old (need 3.12+), port 8000 in use, missing `.env` values.

**ESI data not updating:**
Check the `/status` page for sync errors and rate limit usage. If a character's token is expired, re-authenticate from the dashboard.

**Star map shows a black screen:**
After deploying, do a hard refresh (`Ctrl+Shift+R` / `Cmd+Shift+R`). Cached HTML can reference stale asset hashes.

**"502 Bad Gateway" on VPS:**
The app container may still be starting. Check `docker compose logs app` — the first startup can take a minute to initialize the SDE import.

**Database corruption ("database disk image malformed"):**
Delete the database and restart. You'll need to re-authenticate characters:
```bash
# Local
rm vigilant.db && ./start.sh

# Docker
docker compose down
docker volume rm vigilant_app_data
docker compose up -d --build
```

---

## Contributing

Contributions are welcome:
1. Fork the repository
2. Create a feature branch (`git checkout -b feature/my-feature`)
3. Commit your changes
4. Push and open a Pull Request

---

## License

Vigilant is an independent project provided as-is. EVE Online and all related assets are property of CCP Games. Vigilant is not affiliated with or endorsed by CCP Games.
