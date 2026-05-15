# Phase 6 (Perf & Ops) + Phase 7 (Roadmap & Gaps) — 11 tickets shipped, all 26/28

Continued from yesterday's security burst. Project state went from 15/28 → 26/28 complete this session. Two entire phases closed back-to-back.

## Phase 6 — Performance & Ops (4/4)

| Ticket | Commit | Outcome |
|---|---|---|
| **T-015** | `fcea2af` | `CREATE INDEX IF NOT EXISTS` guard in `init_db()`. 4 silently-missing prod indexes installed: `ix_skill_plans_owner_corp_id`, `ix_skill_plans_owner_alliance_id`, `ix_skill_plans_share_token`, `ix_user_fittings_folder_id`. 66 declared / 0 missing post-deploy. |
| **T-016** | `ee40ae6` | `[0/4]` preflight in `scripts/deploy.sh` verifies external `web` network + compose declaration before touching state. Defends against the "network web declared as external, but could not be found" failure mode that's bitten the deploy twice. |
| **T-014** | `7254134` | Cgroup v2 memory gauge on `/admin` (showing 562/2560 MiB · 22% live). VPS-side `oom-watch.sh` polls `docker events --filter event=oom` every minute via `/etc/cron.d/vigilant-oom-watch`, dedupes via state file, fires Discord `notify()`. Fills the ExitCode 137 ≠ 502 gap. |
| **T-013** | partial | Perf rebaseline — limited window (35min post-T-014 deploy), no alarming routes in natural traffic. Killmail ESI pacing (2 req/s) + disk-first skip both confirmed in code. Filed ISS-013 for follow-up authenticated-traffic baseline. |

## Phase 7 — Roadmap & Gaps (7/7)

| Ticket | Commit | Outcome |
|---|---|---|
| **T-019** | `ea97e9e` | Cleanup: dropped stale `db` arg on `dashboard._client_for` + 11 call sites; removed 4 `wh_kills:` diag logs from wormholes.py. |
| **T-018** | `618778b` | Wormhole kills route: replaced per-ID character/corp/alliance ESI fetches with one bulk `POST /universe/names/` call (up to 1000 IDs, 30-day cache). Was clocked at 15.9s pre-fix; mirrors dashboard kill-pulse pattern. |
| **T-017** | (no-op data) | Wormhole positions regenerated against fresh SDE — diff empty, file was already current. All 2,604 J-systems present including 25 shattered + 5 drifter complexes. Filed ISS-014 for the 37 systems missing community statics (route's `.get(name, [])` fallback already handles it). |
| **T-023** | `c5e4209` + `58d3dfd` | **Major fitting fix**: ship-to-self ItemModifier hull bonuses. Drake `shield_em_resist` 0% → 20% at All V. Affects all HACs, heavy-tank BCs, command ships. Also dropped the no-modules early-return so bare hulls show their traits. Sacrilege modifierInfo override is much bigger scope, filed as ISS-015. |
| **T-020** | `193b84d` | Drone-skill damage bonuses the SDE doesn't propagate. Light/Med/Heavy Drone Op + 4 racial specs + Sentry Drone Interfacing had `damageMultiplierBonus` attrs but no `OwnerRequiredSkillModifier` rows. Hardcoded 9-skill table; Dominix + 5 Ogre II at All V: 118 → 173 DPS (+46% recovery). |
| **T-021** | `9a2b13b` | Damage profile selector + resist-weighted DPS. New `TARGET_RESIST_PROFILES` table; 13-option dropdown in the fitting builder; per-type DPS accumulators in the engine; offense panel shows `raw → effective` when a non-uniform profile is active. Verified: Ogre II × 5 lands 100% under uniform, 45-50% under faction profiles. |
| **T-022** | `e3a6503` + `532799b` + `0fbd6d6` | Implant slots (10 slots) MVP: search endpoint, modifier propagation via `_apply_implant_bonuses` covering OwnerRequired*/LocationGroup/LocationRequired/`ItemModifier+shipID`, UI panel with auto-routing by implantness. Verified: Drake + Eifyr EM-702 align time 12.2s → 11.9s. Saved-fit persistence + clone auto-import deferred to ISS-016. |

## Tickets closed in storybloq this session
T-013, T-014, T-015, T-016, T-017, T-018, T-019, T-020, T-021, T-022, T-023 = **11 tickets**.

## Issues filed during the work
- **ISS-013** — Perf rebaseline incomplete (limited measurement window, no authed traffic exercised)
- **ISS-014** — 37 J-systems missing from wormholes.json:system_statics (community data gap, no user impact)
- **ISS-015** — Sacrilege-class SDE modifierInfo mistargets (deferred from T-023 item 1, needs Pyfa-style effect handler override table)
- **ISS-016** — Implants: persist with saved fits + auto-import from active jump clone (deferred from T-022)

## Memory updates
- `project_fitting_modifier_gaps.md` — items 2 (drone skills) and 6 (ship-to-self bonuses) marked FIXED with commit refs and verified-numbers
- `project_wormhole_data_gaps.md` — updated counts (2,568 of 2,604 covered) + impact (none, route fallback handles it)
- `project_vps_auto_update.md` — added `oom-watch.sh` to the VPS scripts list

## VPS-side changes (not in git, per `feedback_vps_ops_not_in_git`)
- `/etc/vigilant/scripts/oom-watch.sh` (root-owned, mode 0750)
- `/etc/cron.d/vigilant-oom-watch` (per-minute schedule)
- `/var/lib/vigilant/oom-watch.last-seen` (state file)

## Live verification (all post-deploy)
- T-015: 66 declared / 103 in DB / 0 missing; 4 previously-absent indexes present
- T-016: `[0/4] preflight: external 'web' network present; compose declaration intact` fires on every deploy
- T-014: Memory gauge live in /admin (verified 562/2560 MiB · 22% via container exec)
- T-018: `import app.routes.wormholes` clean
- T-017: `wormhole_positions.json` regenerated, no diff (data was current)
- T-023: Drake `shield_em_resist=20%` ✓ (was 0%); Eagle / Damnation / Sacrilege ship-self bonuses all apply
- T-020: Ogre II × 5 = 173 DPS at All V (was ~118 pre-fix)
- T-021: Ogre II × 5 = 173 raw / 87 effective vs Guristas, 78 vs Sansha
- T-022: Drake align time 12.2s → 11.9s with Eifyr EM-702 (agility hardwiring)

## What's left in the project

Only **2 of 28 tickets** still open, both in the Security Hardening phase:

- **T-011** — User-action: run `~/sec-toolkit/bin/sec_audit.py audit vigilant-vps` (or the `/audit` slash command) to flip VVP-2026-001 / VVP-2026-002 / VVP-2026-003 from `fix-in-progress` → `fixed`. Cannot be triggered by an agent.
- **T-012** — CSP `unsafe-inline` removal. Deferred with a 4-step roadmap in the ticket description (nonce middleware → 514 inline-handler refactor → 153 style-attr refactor → flip Report-Only to enforcing). Best taken on as its own multi-session project.

## Notable patterns reinforced this session

- **VPS-only ops scripts stay on the VPS.** OOM watcher installed at `/etc/vigilant/scripts/`, never committed. CLAUDE.md doc-section + memory update were the only repo changes.
- **deploy.sh self-modification gotcha.** The first run after a deploy.sh change uses the OLD logic; the new logic activates on the SECOND deploy. Surfaced again with T-016, behaved as the memory predicted.
- **Pyfa-grade fitting accuracy is iterative.** Two related Pyfa-style overrides (Sacrilege modifierInfo, drone size/spec skills) followed the same shape: SDE has the source attr but no modifier row; need hardcoded handlers. T-020 took the data-driven approach for drone skills; ISS-015 holds the Sacrilege scope for a future session.
- **For ship-self attribute bonuses, ItemModifier+shipID is the canonical pattern.** Same fix shape appeared in 3 different places this session: ship hull traits (T-023), drone skill bonuses don't need it (drones aren't ship-self), implants (T-022). Worth keeping in mind for any future "modifier silently doesn't apply" debug.
