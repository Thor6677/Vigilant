# Vigilant Monitoring Redesign

**Date:** 2026-05-17
**Status:** Approved → Implementation

## Goals

- Detect Vigilant / edge nginx / Wanderer / Portfolio / Renters failures within ~5–10 min.
- Alert on **state transitions** to Discord (no per-tick spam).
- Send recovery notifications with downtime duration.
- Survive a fully-dead VPS via an out-of-band heartbeat (UptimeRobot).
- One shared probe library so the daily digest and the continuous monitor never drift.

## Non-goals

- No TSDB / Grafana / status-page stack.
- No paging escalation beyond Discord role-mention.
- No log aggregation pipeline — keep using `docker logs --since` scans.

## Background — what triggered this

The 2026-05-17 daily-status embed reported `Nginx: 🚨 Down`. Root cause:
`daily-status.sh` checks `vigilant-nginx-1`, but nginx was decoupled into the
shared edge stack on 2026-05-14 (commit 9d1189d) and the container is now
named `edge-nginx-1`. Same stale reference exists in `health-check.sh`. While
fixing that, expand the monitoring surface.

## Existing pieces (do not duplicate)

- `daily-status.sh` — 22:00 UTC digest. Will be refactored to use shared probes.
- `health-check.sh` — invoked post-deploy/post-reboot. Will be refactored.
- `oom-watch.sh` — per-minute OOM event watcher via `docker events`. Untouched.
- `do-api.sh` — exports `notify()` Discord helper. Reused.

## Components

```
       ┌──────────────────────────────────────────────┐
       │      /etc/vigilant/scripts/probes.sh         │  bash library
       └────────┬──────────────────────────────┬──────┘
                │                              │
   ┌────────────▼────────────┐    ┌────────────▼─────────────┐
   │  service-monitor.py     │    │   daily-status.sh        │
   │  every 5 min            │    │   22:00 UTC digest        │
   │  state-change → Discord │    │   refactored to use probes│
   └────────────┬────────────┘    └──────────────────────────┘
                │ heartbeat ping
                ▼
   ┌─────────────────────────┐    ┌──────────────────────────┐
   │  UptimeRobot heartbeat  │    │   UR HTTP monitors        │
   │  (dead-man's switch)    │    │   vigilant + mapper       │
   └──────────────┬──────────┘    └────────┬─────────────────┘
                  │                        │
                  └────────┬───────────────┘
                           ▼
                    Discord webhook
```

### New files (on VPS, `/etc/vigilant/scripts/`)

- **`probes.sh`** — bash library, sourceable. Functions: `probe_container`,
  `probe_http`, `probe_tls`, `probe_disk`, `probe_mem`, `probe_log_errors`.
  Each prints `OK|WARN|CRIT <detail>` on stdout and returns rc 0/1/2.
- **`service-monitor.py`** — Python orchestrator. Owns the probe registry,
  state file, alert state machine, and Discord embed construction.
- **`uptimerobot-setup.py`** — idempotent provisioning script. Creates the
  HTTP and heartbeat monitors via the UptimeRobot API if missing; reuses
  existing monitors keyed by friendly name. Writes the heartbeat URL into
  `/etc/vigilant/auto-update.env`.
- **`maintenance.sh`** — tiny CLI: `maintenance.sh on|off|status`. Touches
  or removes `/var/lib/vigilant/monitor.silenced`. Called by `deploy.sh`
  and `auto-update.sh`.

### Refactored

- **`daily-status.sh`** — sources `probes.sh`. Fix nginx name. Add fields
  for portfolio + renters.
- **`health-check.sh`** — sources `probes.sh`. Fix nginx name.
- **`scripts/deploy.sh`** (in this repo) — wrap the build phase with
  `maintenance on`/`off` so deploy churn doesn't page anyone.

### Systemd

- **`vigilant-service-monitor.service`** — oneshot, runs
  `/etc/vigilant/scripts/service-monitor.py`.
- **`vigilant-service-monitor.timer`** — `OnUnitActiveSec=5min`,
  `Persistent=true`. The first run also serves as a smoke test.

## Probe coverage

| ID | Probe | Max severity | Flap (consecutive fails before alert) |
|---|---|---|---|
| `vigilant-app` | container `vigilant-app-1` running + not unhealthy | CRIT | 1 |
| `vigilant-healthz` | `https://vigilant.thunderborn.dev/healthz` 200 | CRIT | 1 |
| `vigilant-errors` | ERROR/CRITICAL/Traceback in last 5 min (≥1 WARN, ≥5 CRIT) | CRIT | 2 |
| `edge-nginx` | container `edge-nginx-1` running | CRIT | 1 |
| `wanderer-core` | container `wanderer` running | CRIT | 2 |
| `wanderer-kills` | container `wanderer-kills` running | WARN | 2 |
| `wanderer-db` | container `wanderer-wanderer_db-1` running | WARN | 2 |
| `wanderer-routes` | container `eve-route-builder` running | WARN | 2 |
| `wanderer-http` | `https://mapper.thunderborn.dev` 200 | WARN | 2 |
| `portfolio` | container `portfolio` running | WARN | 2 |
| `portfolio-http` | `https://thunderborn.dev` 200 | WARN | 2 |
| `renters` | container `renters-help` running | WARN | 2 |
| `disk-root` | `/` ≥90% WARN / ≥95% CRIT | CRIT | 1 |
| `mem` | RAM ≥85% WARN / ≥95% CRIT | CRIT | 2 |
| `tls-vigilant` | `vigilant.thunderborn.dev:443` ≤30d WARN / ≤14d CRIT | CRIT | 1 |
| `tls-mapper` | `mapper.thunderborn.dev:443` similarly | WARN | 1 |

WARN-only probes have their severity clipped — a CRIT-level result is
downgraded to WARN. Keeps non-essential probes (mapper, portfolio) from
ever pinging the role.

## Alert state machine

State stored at `/var/lib/vigilant/monitor-state.json`:

```jsonc
{
  "vigilant-app": {
    "state": "OK",                 // OK | PENDING | WARN | CRIT
    "detail": "running",           // last probe output (one line)
    "since": 1747500000,           // when current state began
    "last_alerted_at": 0,          // unix ts of last Discord post (0 if OK)
    "fails": 0                     // consecutive failure tick count
  }
}
```

Transitions:

| Prev → Now | Action |
|---|---|
| OK → OK | Update detail. No alert. |
| OK → WARN/CRIT, fails < flap | Move to `PENDING`. No alert. |
| OK → WARN/CRIT, fails ≥ flap | Move to new state. **Alert DOWN**. |
| PENDING → OK | Reset to OK. No alert (was below threshold). |
| PENDING → WARN/CRIT, fails ≥ flap | Promote. **Alert DOWN**. |
| WARN ↔ CRIT (severity change) | Update. **Alert ESCALATE**. |
| WARN/CRIT → WARN/CRIT (same) | Held. **Alert REMIND** if reminder due. |
| WARN/CRIT → OK | Reset. **Alert RECOVERY** with duration. |

**Reminder backoff (ongoing incident):** alerts re-fire at +1h, +2h, +4h,
+8h, +24h, then every +24h. Tracked by comparing `now - since` against
those bucket boundaries and `last_alerted_at` to ensure each bucket fires
at most once.

**Grouping:** if ≥3 transitions fire in the same tick, one combined embed
is sent instead of N (catches "host fell over" cleanly without spamming).

**Role mention:** content includes `<@&1498071616796622868>` only when at
least one transition involves a CRIT state. WARN/RECOVERY embeds are
silent (color only).

**Embed colors:** CRIT 0xE74C3C / WARN 0xFFFF00 / RECOVERY 0x2ECC71.

## Maintenance window

`/var/lib/vigilant/monitor.silenced` flag file. When present:
- The monitor still runs and writes state (recovery transitions remain accurate).
- No Discord posts are sent for transitions.
- The UptimeRobot heartbeat is **still pinged** (deploys shouldn't trip the dead-man's switch).

Auto-expiry: if the silence file is older than 30 min (`mtime`), the
monitor deletes it. Prevents a forgotten `maintenance on` from masking
real outages indefinitely.

`scripts/deploy.sh` wraps:
```
maintenance.sh on
... build / restart ...
maintenance.sh off
```
with a trap so an error path still clears the flag.

## UptimeRobot setup

Provisioned by `uptimerobot-setup.py` (run once, then idempotent on re-run):

1. **HTTP keyword monitor** `vigilant-healthz`
   - URL: `https://vigilant.thunderborn.dev/healthz`
   - Keyword: `ok` (must be present)
   - Interval: 5 min (UR free-tier minimum)
2. **HTTP monitor** `mapper`
   - URL: `https://mapper.thunderborn.dev`
   - Interval: 5 min
3. **HTTP monitor** `portfolio` (optional, low priority)
4. **Heartbeat monitor** `vigilant-monitor-heartbeat`
   - Grace: 15 min (3× run interval; survives one missed tick)
   - URL stored in `UPTIMEROBOT_HEARTBEAT_URL` env var
5. **Alert contact** — Discord webhook (UR native integration).

The internal monitor pings the heartbeat at the end of every successful
run regardless of probe results — the heartbeat says "this script is
alive", not "everything is fine".

## Secrets handling

- `UPTIMEROBOT_API_KEY` and `UPTIMEROBOT_HEARTBEAT_URL` live in
  `/etc/vigilant/auto-update.env` (mode `0600`, root-owned, **not in git**).
- The API key shared during brainstorming is treated as exposed and will
  be rotated post-setup.

## Rollout sequence

1. Add `probes.sh` and `service-monitor.py`. Do **not** enable the timer yet.
2. Manually run `service-monitor.py --dry-run` — prints state, sends no Discord.
3. Manually run `service-monitor.py` once to seed state file with current reality.
4. Install systemd unit + timer. Enable and start the timer.
5. Refactor `daily-status.sh` and `health-check.sh`. Verify daily-status manually:
   `systemctl start vigilant-daily-status.service`.
6. Provision UptimeRobot monitors via `uptimerobot-setup.py`.
7. Modify `scripts/deploy.sh` to bracket with `maintenance.sh on/off`.
8. Commit + push the in-repo changes. Run a deploy to confirm the maintenance
   window suppresses alerts during the container restart.

## Rollback

- Disable the new timer: `systemctl disable --now vigilant-service-monitor.timer`.
- Restore prior `daily-status.sh` from git history if the refactor misbehaves
  (it lives on the VPS but I'll save the original to `daily-status.sh.bak`
  before overwriting).
- `scripts/deploy.sh` change in repo can be reverted via `git revert`.

## Future ideas (parked)

- Anomaly detection on rolling baselines from `/data/logs/perf.log`.
- Public status page (Uptime-Kuma behind edge nginx).
- SQLite backup-freshness probe.
- Self-test mode that intentionally fails one probe to verify alerting end-to-end.
