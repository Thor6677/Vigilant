# Checkpoint — SP2 site-restyle execution + killfeed-search incident (2026-07-03)

## Where we are
Executing `docs/superpowers/plans/2026-07-03-site-restyle-rollout.md` via subagent-driven-development (implementer → spec review → quality review → fixes → re-review per task). Native task IDs 20–27 = plan Tasks 1–8; statuses synced in the co-located `.tasks.json`.

- **Tasks 1–4: DONE and deployed.** Restyle is LIVE on vigilant.thunderborn.dev (deploy commit ebcb840, then batch-A fixes 4c8d458, nav fix 22090e4). Login page = ambient flythrough + glass SSO panel; public endpoints /api/ambient/kills + /api/ambient/systems.json shipped with user approval.
- **Task 5 (sweep batch A): ~90% done.** Checklist at `docs/superpowers/plans/sweep-checklist-2026-07-03.md` (111 templates, batches A–D, risk-annotated). Batch A fixes deployed. User visual pass found 2 issues:
  1. Intel menu missing Watch link → **FIXED + DEPLOYED** (22090e4).
  2. Advanced search 504 → the incident below. NOT a restyle regression.
- **Tasks 6–8 (batches B/C/D): pending**, blocked on Task 5 close.

## The incident (killfeed advanced search 504)
- Root cause (fully established, don't re-investigate): killmails table now ~60M rows / **192 GB** (EVERef backfill overshot the 35GB estimate). SQLite had NO stats (no sqlite_stat1 ever); planner drives `time=7d&space=wh` searches from the system-range index → scans all J-space kills ever (~10M) + temp sort; count query worse (non-covering index, random row reads). >100s → Cloudflare 504. Retries pegged container CPU (114%) until they drained.
- **Code fix committed: 62ac8cc** — `_build_search_statements` helper + SQLite `INDEXED BY ix_killmails_killmail_time` gated on (time lower bound present) ∧ (not live-poll); page stmt additionally requires date sort. Stock SQLAlchemy SQLiteCompiler ignores with_hint → `get_from_hint_text` monkeypatched at module import (verified; nothing else uses with_hint). 16 tests green. **Spec review ✅. Quality review IN FLIGHT** (agent ad0a170fe1f663462) focusing on: monkeypatch discoverability, INDEXED BY hard-error if index renamed, entity+time bounded regression, count-query cost.
- **Prod ANALYZE (belt, separate from fix): still running** inside container (docker exec PID 7027, started ~20:05 UTC, sampled analysis_limit=4000, sqlite 3.46.1); stat1 NOT committed yet; monitor task bmptqbd3b polls every 60s. NOTE: userns remap — docker exec must be `-u vigilant` to write the DB.

## Pending sequence (in order)
1. Quality review verdict → fixes if needed (implementer/fix-agent + re-review loop).
2. `git push origin main` FIRST, then `ssh thunderborn-home "/opt/vigilant/scripts/deploy.sh"` (deploy pulls from GitHub; restart kills the in-container ANALYZE — acceptable).
3. Post-deploy verify: EXPLAIN both search queries on prod → must show `SEARCH killmails USING INDEX ix_killmails_killmail_time (killmail_time>?)` for the 7d+wh shape (read-only pattern: `docker exec -u vigilant vigilant-app-1 python3 -c "...file:/data/vigilant.db?mode=ro..."`).
4. Re-run ANALYZE detached ON THE HOST so it survives sessions: `ssh thunderborn-home "nohup docker exec -u vigilant vigilant-app-1 python3 -c '<analysis_limit=4000; ANALYZE; commit>' >/tmp/analyze.log 2>&1 &"` — then verify stat1 rows > 0 later.
5. User retries advanced search (time=7d & space=wh). On success: mark Task 5 (#24) complete, sync tasks.json, commit checklist updates, proceed to Task 6 (#25, batch B) using the same sweep-batch pattern (dispatch prompt shape = the Task 5 agent prompt).

## Follow-up tickets to file (post-sweep close-out, Task 8)
- Refine hint gate: skip INDEXED BY when a selective entity predicate exists (entity+time searches currently forced onto time index — bounded ms-level regression).
- Count/SUM query: cache or defer (per-request 200-300k-row aggregate even on the right index).
- Query timeout guard for search endpoints (aiosqlite interrupt) so slow queries can't pile up again.
- Periodic `PRAGMA optimize` (or scheduled sampled ANALYZE) so stats don't rot as the table grows.
- `.kf-*` CSS dedup between intel_kills and intel_kills_search; status.html/status_data.html Tailwind-eve restyle decision (user judgment); gate base.html scripts on user_id (slim anonymous page); server-side sov proxy (T-033 CSP list).

## Key operational facts
- Commit → **push** → deploy.sh (pulls from GitHub; deploy-before-push runs old code). rollback.sh = whole-image, safe.
- pytest: `.venv/bin/python -m pytest tests/ -v` (conftest.py provides dummy env; venv at repo root, gitignored).
- The `~/Documents` file-sync creates `" 2"` conflict-copy dirs during heavy writes — check `find <dir> -name "* 2*"` before commits/uploads.
- 31 separate Jinja2Templates instances; template globals pushed via sys.modules loop in app/main.py (css_v cache-buster) — new route modules must name their instance `templates`.
- Design system upstream: `design-system/` (css/ambient/react); claude.ai/design project 36adcd0a-8a7b-46e4-a32d-c746ca48b734 synced; `.design-sync/` holds config/NOTES/previews.
