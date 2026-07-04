# Phase 3 — Quick-Win Features Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers-extended-cc:subagent-driven-development.

**Goal:** Three small, high-value features: Discord relay for user-facing alerts, EVE-Scout Thera/Turnur wormhole shortcuts in routing, and a PWA manifest.

**Model tiering:** Task 1 Sonnet (investigation-first), Task 2 Opus, Task 3 Sonnet, Task 4 coordinator. Baseline suite: 79 passed.

---

### Task 1: Discord alert relay [Sonnet]

**Goal:** Structure attacks, fuel/service alerts, inventory/contract lows, and skill-queue-empty notifications ALSO post to a Discord webhook (browser notifications don't reach a sleeping player).

**Investigate FIRST (report before wiring):** Where are user-facing alert/notification events CREATED (not displayed)? Grep for the notification types visible in base.html's settings panel (`structure_attack`, `structure_fuel`, `inventory_low`, `contract_low`, `skill_complete`, `job_ready`, etc.) — find the single choke point where new notification rows/events are persisted (likely in the sync path in app/routes/dashboard.py or an alerts module). If there's ONE insertion point, wire there; if scattered across >3 sites, implement a small `app/notify/discord.py` helper and call it from the top 2 sites only (structure_attack + structure_fuel — the wake-me-up ones) and report the rest as follow-up.

**Implementation:**
- `app/notify/discord.py`: `async def send_discord_alert(title: str, body: str, alert_type: str)` — POST to webhook URL from settings; 5s timeout; failures logged at warning, NEVER raised into the caller; skip silently if unset. Per-type opt-in via a comma-separated env var `DISCORD_ALERT_TYPES` (default: `structure_attack,structure_fuel`).
- Settings: add `DISCORD_WEBHOOK_URL: str = ""` + `DISCORD_ALERT_TYPES` to the app's existing settings class (find it — get_settings pattern). Document in .env.example if one exists.
- Rate-safety: dedupe repeat sends — keep a module-level `dict[(type, key)] -> last_sent` with a 30-min suppression window so a repeating attack alert doesn't spam.
- Tests: unit-test the helper with a monkeypatched httpx post (sent / suppressed-duplicate / unset-URL no-op / type-not-enabled no-op / failure swallowed+logged).

**Verify:** full suite. Commit: `feat(alerts): Discord webhook relay for structure/fuel alerts`

---

### Task 2: EVE-Scout Thera/Turnur shortcuts in routing [Opus]

**Goal:** The route planner can use live Thera/Turnur wormhole connections (EVE-Scout public API) as graph edges — "via Thera: 6 jumps instead of 23".

**Investigate FIRST:** Where does server-side routing happen? (The star map is a React bundle; find the route-calc endpoints it calls — grep app/routes/starmap.py for route/jump endpoints and the graph structure used.) Determine the cheapest place to inject extra edges.

**Implementation:**
- `app/intel/evescout.py`: fetch `https://api.eve-scout.com/v2/public/signatures` (httpx, 10s timeout, UA with contact email per third-party etiquette memory), parse to `[(system_id_a, system_id_b, wh_ttl_info)]` for Thera + Turnur connections; module-level cache with 10-min TTL (asyncio.Lock-guarded refresh, stale-on-error). No DB table.
- Routing: add optional edges into the routing graph behind a flag param (e.g. `use_evescout=1`) on the existing route endpoint(s); annotate route legs passing through Thera/Turnur so the UI can label them.
- UI: minimal viable surface WITHOUT React changes if possible — if the route planner UI is React, check `frontend/` build (`npm run build` is a proven workflow) and add a simple "Use Thera/Turnur connections" checkbox wired to the flag IF the change is small and the build passes locally; otherwise expose the flag server-side + document, and report the UI wiring as follow-up. Do NOT ship a broken frontend build — if `npm run build` fails or the change sprawls, stop and report.
- Tests: parse fixture JSON (save a small captured sample as a test fixture), TTL-cache behavior (monkeypatched clock/fetch), edge-injection unit test on the graph function.

**Verify:** full suite; if frontend touched, `npm run build` green + built assets committed per repo convention (check how frontend builds are committed — git log for frontend/dist or similar). Commit: `feat(map): EVE-Scout Thera/Turnur connections as optional routing shortcuts`

---

### Task 3: PWA manifest [Sonnet]

**Files:** `static/manifest.json` (name Vigilant, short_name, theme/background colors matching the design system's dark bg, display standalone, start_url /dashboard), icons (generate 192/512 PNGs from static/logo.png via sips/Pillow — check what's available; if logo is non-square, pad on transparent canvas), `<link rel="manifest">` + `theme-color` meta in base.html head, and verify the static mount serves it. Test: manifest fetchable + valid JSON (TestClient), link tag present in rendered page source.

**Verify:** full suite. Commit: `feat(pwa): web app manifest + icons`

---

### Task 4: Deploy + verify Phase 3 [coordinator]

Checklist → push → deploy → logs clean. Prod checks: /static/manifest.json 200; evescout module fetch works from the VPS (one manual fetch, log the connection count); Discord relay — fire a test send via a one-off `docker exec` python snippet IF a webhook URL is configured (it likely is NOT yet — then just verify the no-op path logs nothing and note that the user must set DISCORD_WEBHOOK_URL in the env to activate). Sync tasks.json, close-out commit.
