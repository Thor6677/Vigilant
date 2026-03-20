# Vigilant

An EVE Online dashboard that gives you a unified view of all your characters — wallet, location, industry, orders, skills, mail, and kill history all in one place.

![EVE Online](https://img.shields.io/badge/EVE%20Online-ESI-blue)
![Python](https://img.shields.io/badge/Python-3.11%2B-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115-green)

---

## Features

- **EVE-style character cards** — portrait, corp/alliance logos, current ship (type + name), security status, and diagonal stripe accent matching the in-game UI
- **Multi-character dashboard** — wallet, location, industry jobs, market orders, skill queue, mail, notifications, contracts, planetary industry, and zKillboard kills — all on one page
- **Automatic background sync** — a scheduler runs every 60 seconds, refreshing each field on its ESI-recommended cache timer (30s location, 2min wallet, 1h clones, etc.); page loads never trigger ESI calls
- **Instant on character add** — full sync fires immediately when a character is authenticated
- **Character detail page** — per-character wallet balance chart (with time range selection), wallet journal, active market orders, and kill history
- **Character management** — organize alts into named account groups, reorder with drag-and-drop, view skill queue details with training completion times and paused queue warnings
- **Assets page** — aggregated asset browser across all characters with EVE SDE type lookup and search
- **Corporations page** — corporation-level wallet, industry jobs, market orders, structures, contracts, and member list (requires corp roles)
- **Industry page** — consolidated view of all industry jobs and market orders across all characters
- **Kills page** — aggregated kill history from zKillboard with system security status
- **App status dashboard** — request activity chart (Chart.js), background sync table with expandable per-field warnings, ESI rate limit progress bars, recent request log, and significant event history
- **Sync diagnostics** — per-field ⚠ warnings on the dashboard when a sync fails, with re-authenticate links for expired tokens

---

## Quick Start (Local)

### Prerequisites

- **Python 3.11+** — [python.org](https://www.python.org/)
- **An EVE Online developer application** — [create one here](https://developers.eveonline.com/)
  - Set the callback URL to `http://localhost:8000/auth/callback`
  - You will need the **Client ID** and **Client Secret**

### Installation & Setup

```bash
# Clone the repository
git clone https://github.com/yourusername/vigilant.git
cd vigilant

# Run the startup script
./start.sh
```

The `start.sh` script will:

1. **Check for `.env` file** — Creates one from `.env.example` if it doesn't exist
2. **Generate `SECRET_KEY`** — Auto-generates a secure session signing key
3. **Prompt for EVE credentials** — Asks for your EVE SSO Client ID and Secret if they're not configured
4. **Set up Python environment** — Creates a virtual environment and installs dependencies
5. **Launch Vigilant** — Starts the server in the background
6. **Verify startup** — Waits for the app to confirm it's listening before returning

Vigilant will be available at **http://localhost:8000** once startup is complete.

#### View Logs

```bash
# Stream all logs
tail -f vigilant.log

# Filter for errors and warnings only
tail -f vigilant.log | grep -i 'error\|warning\|critical'
```

#### Stop the App

```bash
./stop.sh
```

Or manually:
```bash
kill $(cat vigilant.pid)
```

---

## Detailed Setup

### 1. EVE Online Developer Application

1. Go to [https://developers.eveonline.com/](https://developers.eveonline.com/)
2. Log in with your EVE Online account (or create one)
3. Click **"Applications"** → **"Create New Application"**
4. Fill in the form:
   - **Application Name**: `Vigilant` (or your preferred name)
   - **Description**: Personal EVE character dashboard
   - **Connection Type**: Select **"Authentication & API Access"**
   - **Permissions**: Select the scope level (see [EVE SSO Scopes](#eve-sso-scopes) below)
5. Set the **Callback URL** to `http://localhost:8000/auth/callback`
6. Create the application and copy your **Client ID** and **Client Secret**

For local/personal use with Vigilant, you can use a broad scope. For VPS deployment, consider restricting to specific scopes for security.

### 2. Configure `.env`

The `start.sh` script will prompt you for credentials interactively, but you can also manually edit `.env`:

```bash
# .env file (required)
EVE_CLIENT_ID=your_eve_client_id
EVE_CLIENT_SECRET=your_eve_client_secret
EVE_CALLBACK_URL=http://localhost:8000/auth/callback
SECRET_KEY=auto-generated-by-start-sh
DATABASE_URL=sqlite+aiosqlite:///./vigilant.db
DEBUG=false
```

All configuration is read from `.env` at startup. Never commit `.env` to git.

### 3. Authentication

1. Start Vigilant: `./start.sh`
2. Navigate to **http://localhost:8000**
3. Click **"Add Character"** or the EVE SSO button
4. You'll be redirected to EVE's login page
5. Authorize Vigilant to access your character
6. You'll be returned to the app with your character added

Characters are stored in the SQLite database. Each character has its own authentication token, which is refreshed as needed.

---

## Configuration

All settings are read from `.env`. Here's a complete reference:

| Variable | Default | Description |
|---|---|---|
| `EVE_CLIENT_ID` | *(required)* | EVE SSO application client ID |
| `EVE_CLIENT_SECRET` | *(required)* | EVE SSO application client secret |
| `EVE_CALLBACK_URL` | `http://localhost:8000/auth/callback` | OAuth callback URL — must match your ESI app config |
| `SECRET_KEY` | *(auto-generated)* | Session cookie signing key — generated by `start.sh` if not present |
| `DATABASE_URL` | `sqlite+aiosqlite:///./vigilant.db` | SQLite database path |
| `DEBUG` | `false` | Enable FastAPI debug mode and `/api/docs` endpoint |

### Environment-Specific Notes

**Local development:**
- Use `DEBUG=true` to enable FastAPI docs and see verbose logs
- `EVE_CALLBACK_URL` should be `http://localhost:8000/auth/callback`

**VPS/Server deployment:**
- Use `DEBUG=false` in production
- Set `EVE_CALLBACK_URL` to your public domain (e.g., `https://vigilant.example.com/auth/callback`)
- Set `SECRET_KEY` to a strong random value (use `python -c "import secrets; print(secrets.token_urlsafe(32))"`)
- Use HTTPS for all endpoints

---

## EVE SSO Scopes

Vigilant requests the following ESI scopes when you authenticate a character:

| Scope | Purpose |
|---|---|
| `esi-wallet.read_character_wallet.v1` | Wallet balance and journal |
| `esi-location.read_location.v1` | Current system location |
| `esi-location.read_ship_type.v1` | Current ship type |
| `esi-assets.read_assets.v1` | Character assets |
| `esi-industry.read_character_jobs.v1` | Industry jobs (manufacturing, research, etc.) |
| `esi-clones.read_clones.v1` | Clone locations |
| `esi-clones.read_implants.v1` | Implant data |
| `esi-markets.read_character_orders.v1` | Buy/sell orders |
| `esi-mail.read_mail.v1` | Mail headers and labels |
| `esi-characters.read_notifications.v1` | In-game notifications |
| `esi-contracts.read_character_contracts.v1` | Contracts |
| `esi-planets.manage_planets.v1` | Planetary interaction data |
| `esi-skills.read_skillqueue.v1` | Skill queue |
| `esi-characters.read_corporation_roles.v1` | Determine if character holds corp roles |
| `esi-corporations.read_corporation_membership.v1` | Corporation member list |
| `esi-wallet.read_corporation_wallets.v1` | Corporation wallet divisions |
| `esi-industry.read_corporation_jobs.v1` | Corporation industry jobs |
| `esi-markets.read_corporation_orders.v1` | Corporation market orders |
| `esi-corporations.read_structures.v1` | Corporation structures |
| `esi-contracts.read_corporation_contracts.v1` | Corporation contracts |
| `esi-assets.read_corporation_assets.v1` | Corporation assets |

Character-level scopes are always requested. Corporation-level scopes are only usable if EVE SSO grants them based on the character's in-game roles. You can view CCP's scope documentation at [EVE Swagger Interface Docs](https://esi.evetech.net/).

---

## Pages

| Route | Description | Refresh Rate |
|---|---|---|
| `/` / `/dashboard` | Main character overview — cards, wallet totals, mail, PI, skill queue, kill history | Auto-syncs every 60s per field |
| `/character/{character_id}` | Individual character detail — wallet chart, journal, assets, orders, kill history | On-demand |
| `/characters` | Manage characters — groups, rename, reorder, remove | On-demand |
| `/assets` | Aggregated asset browser across all characters with SDE type lookup | On-demand |
| `/corporations` | Corporation-level data — members, wallet, industry, orders, structures, contracts | On-demand |
| `/industry` | Consolidated industry jobs and market orders across all characters | 2 minutes |
| `/kills` | Aggregated kill history from zKillboard with system security status | 5 minutes |
| `/status` | App status dashboard — sync health, ESI rate limits, request activity, event history | 3 seconds (real-time) |

---

## Docker / VPS Deployment

Vigilant includes Docker configuration for server deployment.

### Prerequisites for Docker Deployment

- Docker and Docker Compose installed
- A public domain or IP address
- An HTTPS certificate (use Let's Encrypt with nginx reverse proxy)

### Deployment Steps

1. **Copy environment template:**
   ```bash
   cp .env.example .env
   ```

2. **Edit `.env` with your production settings:**
   ```bash
   EVE_CLIENT_ID=your_eve_client_id
   EVE_CLIENT_SECRET=your_eve_client_secret
   EVE_CALLBACK_URL=https://vigilant.example.com/auth/callback
   SECRET_KEY=your-secure-random-key
   DATABASE_URL=sqlite+aiosqlite:///./vigilant.db
   DEBUG=false
   ```

3. **Start with Docker Compose:**
   ```bash
   docker compose up -d
   ```

4. **View logs:**
   ```bash
   docker compose logs -f
   ```

5. **Stop:**
   ```bash
   docker compose down
   ```

### Using `setup_vps.sh`

A helper script is provided for fresh Ubuntu 24.04 VPS setup (run as root):

```bash
chmod +x setup_vps.sh
./setup_vps.sh
```

This will:
- Install Docker and Docker Compose
- Install git
- Create a `vigilant` system user
- Create `/opt/vigilant` and set ownership

After running it, follow the printed next steps to clone the repo, configure `.env`, obtain an SSL certificate, and start the app.

---

## FAQ

### General Questions

**Q: Is my data safe?**

A: Vigilant stores data locally on your machine or server. ESI access and refresh tokens are encrypted at rest in the SQLite database using Fernet symmetric encryption (AES-128-CBC), with a key derived from your `SECRET_KEY` via PBKDF2-SHA256. Session cookies are signed with `SECRET_KEY`. For production, use HTTPS and a strong, randomly generated `SECRET_KEY`.

**Q: Can I use Vigilant with multiple characters?**

A: Yes. You can authenticate multiple characters by clicking "Add Character" and going through the EVE SSO flow again. Characters are grouped and managed from the `/characters` page.

**Q: How often is data refreshed?**

A: The app uses ESI's recommended cache times:
- Location: 30 seconds
- Wallet: 2 minutes
- Skills: 5 minutes
- Industry: 2 minutes
- Markets: 5 minutes
- Clones/Implants: 1 hour
- Mail: 30 seconds

Background sync runs every 60 seconds, refreshing fields whose cache has expired.

**Q: Does this violate EVE Online's Terms of Service?**

A: No. Vigilant is a personal dashboard that uses only official ESI endpoints. It doesn't automate gameplay or interact with the EVE client — it simply provides a better view of your character data. All functionality uses official APIs and complies with CCP's guidelines.

**Q: Can I host Vigilant on a VPS?**

A: Yes. Use the Docker deployment steps above. Make sure to:
- Use HTTPS with a valid certificate
- Set `EVE_CALLBACK_URL` to your public domain
- Keep your `.env` secure (don't share or commit to git)

### Troubleshooting

**Q: Vigilant won't start ("App did not confirm startup within 10 seconds")**

A: Check the logs:
```bash
tail -30 vigilant.log
```

Common causes:
- Python version too old (need 3.11+)
- Missing dependencies (run `pip install -r requirements.txt` in `.venv`)
- Port 8000 already in use (change `--port` in `start.sh` or kill the other process)
- Database locked (delete `vigilant.db` and restart)

**Q: "connection refused" when accessing http://localhost:8000**

A: The app may not be running. Check:
```bash
ps aux | grep uvicorn
cat vigilant.log  # Check for startup errors
./start.sh           # Start it again
```

**Q: ESI data isn't updating**

A: Check the `/status` page:
- Look for warnings in the sync table
- Check ESI rate limit usage
- Check character authentication status

If a character has an expired token, click the "Re-authenticate" link on the dashboard.

**Q: How do I remove a character?**

A: Go to `/characters` and click the remove (X) button next to the character. This will:
- Delete the character from your database
- Remove stored tokens
- Remove cached ESI data

**Q: Can I change my EVE SSO credentials?**

A: Yes. Edit `.env` and update `EVE_CLIENT_ID` and `EVE_CLIENT_SECRET`, then restart the app.

**Q: Database file got corrupted ("database disk image malformed")**

A: Delete the `.db` file and restart:
```bash
rm vigilant.db
./start.sh
```

You'll need to re-authenticate your characters.

**Q: How do I upgrade to a new version?**

A: Pull the latest code and restart:
```bash
./stop.sh
git pull origin main
./start.sh
```

The app will automatically migrate the database schema if needed.

---

## Security

### Authentication & Authorization

- **EVE Online SSO** — Login is handled entirely by CCP's official OAuth2 SSO. No passwords are stored by Vigilant.
- **User isolation** — Every database query is scoped to the authenticated user's `user_id`. One user cannot access another user's characters, assets, or ESI data.
- **Character ownership** — A character already claimed by one account cannot be added to another.
- **State validation** — OAuth callbacks validate a CSRF-like state token stored in the session to prevent redirect hijacking.

### ESI Token Encryption (At Rest)

ESI access tokens and refresh tokens are encrypted in the SQLite database using **Fernet symmetric encryption** (AES-128-CBC with HMAC-SHA256 authentication):

- The encryption key is derived from your `SECRET_KEY` using **PBKDF2-SHA256** (100,000 iterations) with a fixed application salt.
- Encryption and decryption are transparent — implemented as a SQLAlchemy `TypeDecorator` on the `access_token` and `refresh_token` columns. No application code outside `app/db/encryption.py` handles raw token bytes.
- On startup, any plaintext tokens from older versions are detected and automatically encrypted in-place.
- **Important:** If `SECRET_KEY` changes, all stored tokens become unreadable. Users would need to re-authenticate via EVE SSO. Never change `SECRET_KEY` in a running deployment without first decrypting tokens.

### Session Security

- Sessions are signed with `SECRET_KEY` using `itsdangerous` (via Starlette's `SessionMiddleware`).
- Session cookies are `HttpOnly`, `SameSite=Lax`, and `Secure` (HTTPS-only in production).
- Sessions expire after 30 days.
- `SECRET_KEY` is auto-generated by `start.sh` if not set — use a minimum of 32 random bytes for production.

### Transport Security (HTTPS)

The included nginx configuration enforces:
- HTTP → HTTPS redirect (301)
- TLS 1.2 and 1.3 only
- `Strict-Transport-Security` (HSTS, 1 year)
- `X-Frame-Options: DENY`
- `X-Content-Type-Options: nosniff`

### Docker Hardening

The production Docker container runs with:
- `read_only: true` filesystem
- `no-new-privileges: true`
- `cap_drop: ALL`
- Memory (512MB), CPU (0.5), and PID limits
- No host-mounted volumes for application code

### Production Checklist

1. **HTTPS** — use a valid TLS certificate; set `EVE_CALLBACK_URL` to your `https://` domain
2. **`SECRET_KEY`** — generate with `python -c "import secrets; print(secrets.token_urlsafe(64))"` and never change it while the database has active users
3. **`.env` permissions** — `chmod 600 .env`; never commit to git
4. **Database backups** — the SQLite file (`/data/vigilant.db` in Docker) should be backed up regularly
5. **`DEBUG=false`** — disables the `/api/docs` endpoint in production

### Data Privacy

- **Your character data is stored locally** — not sent to external servers (except EVE's ESI and zKillboard for public kill data)
- **No telemetry or analytics** — Vigilant doesn't track usage or send data anywhere
- **Open source** — all code is transparent and auditable

### Reporting Security Issues

If you discover a security vulnerability, **do not open a public issue**. Instead:
1. Email the project maintainer with details
2. Provide time for a patch before public disclosure
3. Do not exploit the vulnerability for personal gain

---

## Tech Stack

- **Backend**: FastAPI, SQLAlchemy (async/aiosqlite), Uvicorn
- **Frontend**: Jinja2 templates, HTMX, Tailwind CSS (CDN), Chart.js
- **Data**: EVE ESI REST API, zKillboard API, EVE SDE (Static Data Export)
- **DevOps**: Docker, Docker Compose, systemd (optional)

---

## Contributing

Vigilant is open source and contributions are welcome:
1. Fork the repository
2. Create a feature branch (`git checkout -b feature/my-feature`)
3. Commit your changes
4. Push to the branch
5. Open a Pull Request

Please include tests and update documentation for new features.

---

## License

Vigilant is an independent project. EVE Online and all related assets are property of CCP Games.

The Vigilant code is provided as-is. Use at your own risk.

---

## Troubleshooting Tips

- **Check logs frequently**: `tail -f vigilant.log`
- **Monitor the `/status` page** for sync errors and ESI rate limits
- **Test EVE SSO**: Log into [https://esi.evetech.net/](https://esi.evetech.net/) to verify your app is registered
- **Verify Python version**: `python --version` (must be 3.11+)
- **Clean reinstall**: `rm -rf .venv vigilant.db && ./start.sh`

---

## Getting Help

- Check the [FAQ](#faq) section above
- Review logs in `vigilant.log`
- Check the `/status` page for sync errors
- Open an issue on GitHub with logs and error details

**Vigilant is not affiliated with or endorsed by CCP Games.**
