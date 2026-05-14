# Storybloq Setup Handover

**Date:** 2026-05-13
**Session goal:** Bootstrap storybloq tracking for the existing vigilant-vps codebase.

## What was created

- 8 phases (p1–p5 marked complete via placeholder tickets; p6/p7/p8 active)
- 23 work tickets across active phases (5 placeholder tickets bring total to 28)
- 2 blockedBy edges:
  - `T-002` (dep bump) blocked by `T-001` (TemplateResponse migration) — root-cause split of the 2026-04-28 outage
  - `T-011` (sec-toolkit /audit) blocked by `T-003` and `T-006` (verifications must run first)
- Initial snapshot saved
- `.gitignore` updated with `.story/snapshots/`, `.story/sessions/`, `.story/status.json`

## Setup decisions captured

These answers shape how `/story` interprets work going forward.

- **Surface:** Web app (deployed)
- **Characteristics:** Realtime-ish background sync; no AI; not a marketplace
- **Shape:** Monolith — FastAPI app + bundled React frontend, behind nginx
- **Execution model:** User-facing requests + background ESI sync scheduler
- **Stack:** Python 3.12 / FastAPI 0.115 / SQLAlchemy 2.0 (async) / aiosqlite / Jinja2+htmx / React 19 + Vite + Pixi.js v8
- **Deployment:** Self-hosted Docker Compose on a DigitalOcean VPS, behind nginx reverse proxy. Live at `https://vigilant.thunderborn.dev`. Co-hosts the Wanderer mapper via shared external Docker network `web`.
- **Data model:** SQLite single-file + EVE SDE static tables. Per-user scoping enforced at query layer.
- **Domain complexity:** Workflows for industry/PI; multi-user with role-based admin/manager/user.
- **Auth model:** EVE Online SSO (OAuth2), Fernet-encrypted ESI tokens at rest. First-user-auto-admin is open as a privilege-escalation issue (T-007).
- **Sensitive domain:** No (game data, not regulated). Token storage still treated as sensitive — encryption + HTTPS enforced.
- **Quality checks for autonomous mode:** Minimal recipe (no test suite exists; deployments are gated by VPS smoke + `:prev` rollback tag). Can raise later if a test suite is introduced.
- **CLAUDE.md:** NOT regenerated — existing `CLAUDE.md` is authoritative and richer than the setup flow would produce.
- **RULES.md:** Not generated. Existing CLAUDE.md "Key Gotchas" section already serves that role.

## Why the 23 active tickets exist

The proposal went through one round of independent review, which surfaced 10 missing tickets and 2 wrong dependency edges. Key insights baked into the result:

1. **`SECURITY_TODO.md` had ~10 findings the first-pass proposal missed.** p6 now covers all open/fix-in-progress sec-toolkit items, plus a single "run /audit to close fix-in-progress" bookkeeping ticket (T-011).
2. **T-002 (dep bump) and T-003 (non-root container) are kept independent** — bundling them caused a 22-min prod outage on 2026-04-28. Each ticket carries a "MUST NOT co-merge" note.
3. **T-001 split out from the dep bump.** The TemplateResponse signature migration is its own deploy with its own verification; only after that lands cleanly can the Starlette 0.49+ dep bump go in.
4. **T-018 (Sacrilege modifierInfo + ship-to-self hull resists)** was a high-impact accuracy bug the first pass missed — surfaced by the review against `project_fitting_modifier_gaps.md`.
5. **T-015 (CREATE INDEX guard) and T-016 (web network preflight)** were added from `feedback_create_all_skips_indexes.md` and `wanderer_cohost.md` — both have already bitten deploys.

## What to do next session

- Type `/story` to load context and see ranked work suggestions
- Type `/story auto` to let me work through tickets autonomously (uses Minimal quality recipe by default)
- Most impactful next ticket is probably T-001 (TemplateResponse migration) — it unblocks T-002 (dep bump VVP-001) which is the longest-standing open security finding

## Notes for future sessions

- The 5 placeholder tickets (T-024–T-028) marked `complete` exist solely to make storybloq derive phase status as complete. They have no work content.
- New tickets numbered from T-029 onward.
- If real follow-up work emerges for a "complete" phase (p1–p5), create a new ticket in that phase and unmark the placeholder — better than retro-fitting the placeholder's description.
- The project already has a `.claude/` directory and CLAUDE.md with rich session memory (see `/Users/iianjohnson/.claude/projects/-Users-iianjohnson-Documents-Personal-vigilant-vps/memory/MEMORY.md`). Storybloq is additive context, not a replacement.
