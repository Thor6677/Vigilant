# thunderborn-ops Repo Extraction

**Date:** 2026-05-17
**Status:** Approved scope → ready to execute on your sign-off

## Goal

Move host-level operational scripts out of the vigilant codebase into a
dedicated `thunderborn-ops` repo. Names should match what each script
actually monitors — most of `/etc/vigilant/scripts/` watches vigilant +
wanderer + portfolio + renters + edge nginx, not just vigilant.

## Final layout

```
~/Documents/Personal/thunderborn-ops/        # new git repo
├── README.md
├── CLAUDE.md
├── install.sh                  # idempotent bootstrap (units, dirs, perms)
├── scripts/
│   ├── auto-update.sh
│   ├── daily-status.sh
│   ├── do-api.sh
│   ├── maintenance.sh
│   ├── oom-watch.sh
│   ├── post-reboot-check.sh
│   ├── probes.sh
│   ├── service-monitor.py
│   └── uptimerobot-setup.py
├── systemd/
│   ├── hostops-auto-update.service
│   ├── hostops-auto-update.timer
│   ├── hostops-daily-status.service
│   ├── hostops-daily-status.timer
│   ├── hostops-service-monitor.service
│   ├── hostops-service-monitor.timer
│   └── hostops-post-reboot-check.service
└── cron/
    └── hostops-oom-watch        # minute-cadence OOM watcher
```

On the VPS:

| Old path | New path |
|---|---|
| `/etc/vigilant/scripts/` | `/opt/thunderborn-ops/scripts/` (git checkout, the live install) |
| `/etc/vigilant/auto-update.env` | `/etc/host-ops/host-ops.env` (secrets, mode 0600, not in git) |
| `/etc/vigilant/.pending-reboot-*` | `/etc/host-ops/.pending-reboot-*` |
| `/var/lib/vigilant/` | `/var/lib/host-ops/` |
| `/var/log/vigilant-monitor.log` | `/var/log/hostops-monitor.log` |
| `/var/log/vigilant-auto-update.log` | `/var/log/hostops-auto-update.log` |
| `/etc/systemd/system/vigilant-*.{service,timer}` | `/etc/systemd/system/hostops-*.{service,timer}` |
| `/etc/cron.d/vigilant-oom-watch` | `/etc/cron.d/hostops-oom-watch` |

## What stays in vigilant

| File | Why |
|---|---|
| `scripts/deploy.sh` | vigilant-app deploy specifically |
| `scripts/rollback.sh` | vigilant-app rollback |
| Pre-existing `health-check.sh` reference | Used by `deploy.sh`, gates vigilant-app health |

Vigilant's `health-check.sh` is the only script that needs to source
`thunderborn-ops/probes.sh` after the split. It'll reference
`/opt/thunderborn-ops/scripts/probes.sh`. Cross-repo dependency, but
acceptable — thunderborn-ops is always installed on this VPS.

`deploy.sh` also references `/etc/vigilant/scripts/maintenance.sh` —
that path needs updating to `/opt/thunderborn-ops/scripts/maintenance.sh`.

## What does NOT rename

Externals that touch other systems are out of scope:

- Discord webhook display name (`Vigilant`) and role mention
  (`@vigilant-notification`) — these are Discord-side; renaming them in
  Discord is independent and can happen later via the Discord UI.
- Docker container names (`vigilant-app-1`, `edge-nginx-1`, etc.).
- The DigitalOcean droplet hostname.

Scripts continue to *reference* `vigilant-app-1` etc. — that's correct,
those are still the names of the things being watched.

## Migration phases

### Phase 1 — Build the repo locally

1. `git init ~/Documents/Personal/thunderborn-ops`
2. Copy current scripts from VPS via SCP into `scripts/`.
3. Run a path-replace pass — every script needs its internal references
   updated. Mapping:
   - `/etc/vigilant/scripts/` → `/opt/thunderborn-ops/scripts/`
   - `/etc/vigilant/auto-update.env` → `/etc/host-ops/host-ops.env`
   - `/etc/vigilant/.pending-reboot-` → `/etc/host-ops/.pending-reboot-`
   - `/var/lib/vigilant/` → `/var/lib/host-ops/`
   - `/var/log/vigilant-monitor.log` → `/var/log/hostops-monitor.log`
   - `/var/log/vigilant-auto-update.log` → `/var/log/hostops-auto-update.log`
4. Write the seven systemd units with the new names + new ExecStart paths.
5. Write `install.sh` (idempotent: creates dirs, copies units, runs
   `daemon-reload`, installs cron file).
6. Write `CLAUDE.md` for the new repo — short, mirrors vigilant's style.
7. Initial commit.

### Phase 2 — Cutover on the VPS

Done in one session, with timers stopped to avoid mid-rename ticks.

1. **Stop everything:**
   ```
   sudo systemctl stop vigilant-service-monitor.timer
   sudo systemctl stop vigilant-daily-status.timer
   sudo systemctl stop vigilant-auto-update.timer
   sudo systemctl disable vigilant-service-monitor.timer \
        vigilant-daily-status.timer vigilant-auto-update.timer
   sudo rm /etc/cron.d/vigilant-oom-watch
   ```
2. **Set maintenance flag** so no alerts fire during the move:
   `sudo /etc/vigilant/scripts/maintenance.sh on`
3. **Create directories:**
   ```
   sudo mkdir -p /etc/host-ops /var/lib/host-ops /opt/thunderborn-ops
   ```
4. **Move state files** (preserving content):
   ```
   sudo mv /var/lib/vigilant/monitor-state.json   /var/lib/host-ops/
   sudo mv /var/lib/vigilant/oom-watch.last-seen  /var/lib/host-ops/ 2>/dev/null || true
   sudo mv /etc/vigilant/auto-update.env          /etc/host-ops/host-ops.env
   sudo chmod 600 /etc/host-ops/host-ops.env
   ```
   Marker files (`/etc/vigilant/.pending-reboot-*`) only exist during a
   reboot window — they're not present right now (verified pre-migration),
   so no migration needed; the new path takes effect on next reboot cycle.
5. **Clone the new repo** to `/opt/thunderborn-ops/`:
   ```
   sudo git clone <github-url> /opt/thunderborn-ops
   ```
6. **Run install.sh** — copies units to `/etc/systemd/system/`, copies
   cron file, runs `daemon-reload`.
7. **Smoke test:**
   ```
   sudo /opt/thunderborn-ops/scripts/service-monitor.py --dry-run
   sudo /opt/thunderborn-ops/scripts/service-monitor.py --status
   ```
   Both should show all 16 probes OK (state file migrated cleanly).
8. **Enable new timers:**
   ```
   sudo systemctl enable --now hostops-service-monitor.timer
   sudo systemctl enable --now hostops-daily-status.timer
   sudo systemctl enable --now hostops-auto-update.timer
   ```
9. **Clear maintenance flag:**
   `sudo /opt/thunderborn-ops/scripts/maintenance.sh off`
10. **Remove old directory tree:**
    ```
    sudo rm -rf /etc/vigilant/scripts
    sudo rmdir /etc/vigilant /var/lib/vigilant 2>/dev/null || true
    sudo rm /etc/systemd/system/vigilant-*.service /etc/systemd/system/vigilant-*.timer
    sudo systemctl daemon-reload
    ```

### Phase 3 — Update the vigilant repo

1. Edit `scripts/deploy.sh`:
   `MAINTENANCE=/etc/vigilant/scripts/maintenance.sh` →
   `MAINTENANCE=/opt/thunderborn-ops/scripts/maintenance.sh`
2. Edit `scripts/health-check.sh` (lives on VPS at `/etc/vigilant/scripts/health-check.sh`):
   `source "$SCRIPT_DIR/probes.sh"` →
   `source /opt/thunderborn-ops/scripts/probes.sh`
   Actually `health-check.sh` is also in `/etc/vigilant/scripts/` currently —
   confirm: does it stay there (vigilant-app-specific) or move?
   **Decision:** keeps living on the VPS as part of vigilant's deploy
   path, but is NOT in either git repo (this matches today's state where
   it lives only on disk). Its `probes.sh` reference updates to
   `/opt/thunderborn-ops/scripts/probes.sh`.
3. Edit `CLAUDE.md`:
   - Drop sections about VPS-wide ops scripts.
   - Add a pointer: "Host ops live at `~/Documents/Personal/thunderborn-ops/`
     and `/opt/thunderborn-ops/` on the VPS."
4. Update memory entries:
   - `feedback_vps_ops_not_in_git.md` is now obsolete — delete the file
     and remove its line from `MEMORY.md`.
   - `reference_edge_stack.md` may mention `/etc/vigilant/scripts/` —
     update path references.
5. Commit + push: `chore(scope): extract host ops to thunderborn-ops repo`.

### Phase 4 — Verify

- `journalctl -u hostops-service-monitor.service --since "5 min ago"` —
  next tick fires under the new unit name.
- `sudo /opt/thunderborn-ops/scripts/service-monitor.py --test-alert` —
  posts one test embed to Discord. Confirms the webhook env reload works
  under the new path.
- Trigger `sudo systemctl start hostops-daily-status.service` — daily
  status embed should appear in Discord.
- Wait 5 min, confirm timer ticks and state file at
  `/var/lib/host-ops/monitor-state.json` updates.

## Rollback

If something breaks during cutover:

1. `sudo systemctl stop hostops-*.timer`
2. `sudo mv /etc/host-ops/host-ops.env /etc/vigilant/auto-update.env`
3. `sudo mv /var/lib/host-ops/monitor-state.json /var/lib/vigilant/`
4. `sudo systemctl enable --now vigilant-service-monitor.timer …`

The old units in `/etc/systemd/system/` aren't deleted until the very
last step of Phase 2, so rollback is just reversing the stop/disable.

## Open questions resolved

- **Repo name:** `thunderborn-ops` ✓
- **Rename scope:** internal paths + units + logs + env. External (Discord,
  containers) stays. ✓
- **GitHub remote:** TBD — push to a new private repo under your account
  before Phase 2? Or stay local-only like `/opt/edge/`? Default: create
  the GitHub repo so you have backup + history; mark it private.

## Notes

- The Discord role mention (`<@&1498071616796622868>`) is hardcoded
  in two places (`do-api.sh` `$DISCORD_ROLE` and `service-monitor.py`
  `ROLE_MENTION`). After the move, optionally rename the Discord role
  to `@hostops-notifications` in the Discord UI; the role ID stays the
  same so no code change is needed.
- `health-check.sh` is the only vigilant-specific script that doesn't
  live in either git repo. Worth committing it to vigilant's repo at
  `scripts/health-check.sh` as part of this work — currently it's only
  on the VPS, which is fragile.
