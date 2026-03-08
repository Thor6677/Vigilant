# CapsuleerAI

An AI-powered EVE Online assistant that gives you a unified dashboard for all your characters and a conversational interface to query your in-game data.

![EVE Online](https://img.shields.io/badge/EVE%20Online-ESI-blue)
![Python](https://img.shields.io/badge/Python-3.11%2B-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115-green)

---

## Features

- **Multi-character dashboard** — wallet, location, industry jobs, market orders, skill queue, mail, notifications, contracts, planetary industry, and zKillboard kills — all on one page
- **Background sync** — data refreshes per-field on ESI-recommended cache timers (30s for location, 2min for wallet, etc.) without blocking the UI
- **AI chat assistant (AURA)** — ask questions about your characters in plain English; backed by Claude (Anthropic) or any Ollama-compatible local model
- **Character grouping** — organize alts into named account groups, reorder with drag-and-drop
- **Skills page** — full skill queue details with training completion times and warnings for paused queues
- **ESI status page** — live ESI health, rate limit event log, and recent API request history
- **Sync diagnostics** — per-field ⚠ warnings on the dashboard when a sync fails, with re-authenticate links for expired tokens

---

## Quick Start (local)

### Prerequisites

- Python 3.11+
- An EVE Online developer application ([create one here](https://developers.eveonline.com/))
  - Set the callback URL to `http://localhost:8000/auth/callback`
- One of:
  - **Anthropic API key** (for Claude) — [console.anthropic.com](https://console.anthropic.com)
  - **Ollama** running locally with a model pulled (e.g. `ollama pull qwen3:32b`)

### Install & run

```bash
git clone https://github.com/yourusername/capsuleerai.git
cd capsuleerai
./start.sh
```

`start.sh` will:
1. Create a `.env` file and prompt for your EVE SSO credentials and (if using Anthropic) your API key
2. Create a Python virtual environment and install dependencies
3. Start the app in the background at **http://localhost:8000**
4. Confirm the app started successfully

To stop:
```bash
./stop.sh
```

To watch logs:
```bash
tail -f capsuleerai.log
tail -f capsuleerai.log | grep -i 'error\|warning\|critical'
```

---

## Configuration

All settings are read from `.env`. Key variables:

| Variable | Default | Description |
|---|---|---|
| `EVE_CLIENT_ID` | *(required)* | EVE SSO application client ID |
| `EVE_CLIENT_SECRET` | *(required)* | EVE SSO application client secret |
| `EVE_CALLBACK_URL` | `http://localhost:8000/auth/callback` | OAuth callback URL |
| `SECRET_KEY` | *(auto-generated)* | Session cookie signing key |
| `LLM_PROVIDER` | `ollama` | `ollama` or `anthropic` |
| `ANTHROPIC_API_KEY` | | Required when `LLM_PROVIDER=anthropic` |
| `ANTHROPIC_MODEL` | `claude-sonnet-4-6` | Anthropic model ID |
| `OLLAMA_BASE_URL` | `http://localhost:11434/v1` | Ollama OpenAI-compatible endpoint |
| `OLLAMA_MODEL` | `qwen3:32b` | Ollama model name |
| `DATABASE_URL` | `sqlite+aiosqlite:///./capsuleerai.db` | SQLite database path |
| `DEBUG` | `false` | Enable FastAPI debug mode and `/api/docs` |

---

## EVE SSO Scopes

CapsuleerAI requests the following ESI scopes when you add a character:

- `esi-wallet.read_character_wallet.v1`
- `esi-location.read_location.v1` / `read_ship_type.v1` / `read_online.v1`
- `esi-industry.read_character_jobs.v1`
- `esi-clones.read_clones.v1` / `read_implants.v1`
- `esi-markets.read_character_orders.v1`
- `esi-mail.read_mail.v1`
- `esi-characters.read_notifications.v1`
- `esi-contracts.read_character_contracts.v1`
- `esi-planets.manage_planets.v1`
- `esi-skills.read_skills.v1` / `read_skillqueue.v1`
- `esi-corporations.read_corporation_membership.v1` *(optional, for corp roles)*

---

## Docker / VPS deployment

A `Dockerfile`, `docker-compose.yml`, and `setup_vps.sh` are included for server deployment. Copy `.env.example` to `.env`, fill in your credentials, then:

```bash
docker compose up -d
```

---

## Tech Stack

- **Backend**: FastAPI, SQLAlchemy (async/aiosqlite), Uvicorn
- **Frontend**: Jinja2 templates, HTMX, Tailwind CSS (CDN)
- **AI**: Anthropic Claude API or Ollama (OpenAI-compatible)
- **Data**: EVE ESI REST API, zKillboard API, EVE SDE (Static Data Export)

---

## License

This project is for personal use. EVE Online and all related assets are property of CCP Games.
