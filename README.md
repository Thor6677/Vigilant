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
- **Character detail pages** — individual character overview with location, assets, orders, and kill history
- **Industry page** — consolidated view of all industry jobs and market orders across all characters
- **Kills page** — aggregated kill history from zKillboard with system security status
- **Character grouping** — organize alts into named account groups, reorder with drag-and-drop
- **Skills page** — full skill queue details with training completion times and warnings for paused queues
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
git clone https://github.com/yourusername/capsuleerai.git
cd capsuleerai

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
tail -f capsuleerai.log

# Filter for errors and warnings only
tail -f capsuleerai.log | grep -i 'error\|warning\|critical'
```

#### Stop the App

```bash
./stop.sh
```

Or manually:
```bash
kill $(cat capsuleerai.pid)
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
| `esi-wallet.read_character_wallet.v1` | Read wallet balance and transaction history |
| `esi-location.read_location.v1` | Current system location |
| `esi-location.read_ship_type.v1` | Current ship type information |
| `esi-location.read_online.v1` | Character online status |
| `esi-industry.read_character_jobs.v1` | Industry jobs (manufacturing, research, etc.) |
| `esi-clones.read_clones.v1` | Clone locations and attributes |
| `esi-clones.read_implants.v1` | Implant data |
| `esi-markets.read_character_orders.v1` | Sell/buy orders |
| `esi-mail.read_mail.v1` | Mail headers and labels |
| `esi-characters.read_notifications.v1` | In-game notifications |
| `esi-contracts.read_character_contracts.v1` | Contract information |
| `esi-planets.manage_planets.v1` | Planetary interaction data |
| `esi-skills.read_skills.v1` | Trained skills |
| `esi-skills.read_skillqueue.v1` | Skill queue status |
| `esi-corporations.read_corporation_membership.v1` | Corporation membership (optional) |

All scopes are requested at authentication. You can view CCP's scope documentation at [EVE Swagger Interface Docs](https://esi.evetech.net/).

---

## Pages

| Route | Description | Refresh Rate |
|---|---|---|
| `/` / `/dashboard` | Main character overview — cards, wallet totals, mail, PI, skill queue, kill history | Auto-syncs every 60s per field |
| `/skills` | Detailed skill queue for all characters with training times and paused queue warnings | 1 minute |
| `/characters` | Manage characters — groups, rename, reorder, remove | On-demand |
| `/industry` | Consolidated industry jobs and market orders across all characters | 2 minutes |
| `/kills` | Aggregated kill history from zKillboard with system security status | 5 minutes |
| `/character/{character_id}` | Individual character detail — location, assets, orders, contract summary | 1 minute |
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

A helper script is provided for VPS setup:

```bash
chmod +x setup_vps.sh
./setup_vps.sh
```

This will guide you through:
- Installing dependencies
- Configuring `.env`
- Setting up systemd service (optional)
- Configuring nginx reverse proxy (optional)

---

## FAQ

### General Questions

**Q: Is my data safe?**

A: Vigilant stores data locally on your machine or server. Your ESI tokens are stored in the SQLite database with no encryption (acceptable for local use). For production servers, consider adding database encryption. Authentication tokens are stored securely and rotated as needed.

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
tail -30 capsuleerai.log
```

Common causes:
- Python version too old (need 3.11+)
- Missing dependencies (run `pip install -r requirements.txt` in `.venv`)
- Port 8000 already in use (change `--port` in `start.sh` or kill the other process)
- Database locked (delete `capsuleerai.db` and restart)

**Q: "connection refused" when accessing http://localhost:8000**

A: The app may not be running. Check:
```bash
ps aux | grep uvicorn
cat capsuleerai.log  # Check for startup errors
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

### Local Use (Development)

For personal, local-only use:
- ESI tokens stored plaintext in SQLite — acceptable for local use
- Session cookies signed with auto-generated `SECRET_KEY`
- HTTPS not required for localhost

### Production/Server Deployment

For VPS or public deployment, follow these practices:

1. **HTTPS Only**
   - Use a valid SSL/TLS certificate (Let's Encrypt is free)
   - Configure via nginx reverse proxy or similar
   - Set `EVE_CALLBACK_URL` to your `https://` domain

2. **Secure `.env`**
   - Never commit `.env` to git
   - Restrict file permissions: `chmod 600 .env`
   - Use strong, random values for `SECRET_KEY` and API keys
   - Store secrets in environment variables, not files (if using container orchestration)

3. **Database Security**
   - SQLite is suitable for personal use but consider PostgreSQL for production
   - Restrict database file permissions: `chmod 600 capsuleerai.db`
   - Regular backups of your database
   - For sensitive deployments, add database encryption (e.g., SQLCipher)

4. **Token Management**
   - ESI tokens are refreshed automatically when they expire
   - Tokens are not logged or exposed in debug output
   - If you suspect a token is compromised, revoke it from EVE's account management

5. **API Rate Limiting**
   - EVE ESI has strict rate limits (100-1200 requests per second depending on auth status)
   - Vigilant respects these limits and queues requests
   - Monitor the `/status` page for rate limit warnings

6. **Reverse Proxy Setup (Recommended)**

   Example nginx configuration:
   ```nginx
   upstream vigilant {
       server localhost:8000;
   }

   server {
       listen 443 ssl http2;
       server_name vigilant.example.com;

       ssl_certificate /path/to/cert.pem;
       ssl_certificate_key /path/to/key.pem;

       location / {
           proxy_pass http://vigilant;
           proxy_set_header Host $host;
           proxy_set_header X-Real-IP $remote_addr;
           proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
           proxy_set_header X-Forwarded-Proto $scheme;
       }
   }
   ```

7. **Network Security**
   - Run behind a firewall
   - Only expose the necessary ports (443 for HTTPS)
   - Consider requiring authentication before the EVE SSO flow

### Data Privacy

- **Your character data is stored locally** — not sent to external servers (except EVE's ESI)
- **zKillboard data** is publicly available kill history, aggregated from player kills
- **No telemetry or analytics** — Vigilant doesn't track usage or send data home
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

- **Check logs frequently**: `tail -f capsuleerai.log`
- **Monitor the `/status` page** for sync errors and ESI rate limits
- **Test EVE SSO**: Log into [https://esi.evetech.net/](https://esi.evetech.net/) to verify your app is registered
- **Verify Python version**: `python --version` (must be 3.11+)
- **Clean reinstall**: `rm -rf .venv vigilant.db && ./start.sh`

---

## Getting Help

- Check the [FAQ](#faq) section above
- Review logs in `capsuleerai.log`
- Check the `/status` page for sync errors
- Open an issue on GitHub with logs and error details

**Vigilant is not affiliated with or endorsed by CCP Games.**
