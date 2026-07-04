# Phase 0 — Bug Fixes & Hygiene Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the two confirmed shared-AsyncSession concurrency bugs, bound the two unbounded killmail-table scans, add logging to silently-swallowed errors, throttle the fittings ESI fan-out, and migrate the last two legacy-styled pages to the design system.

**Architecture:** Point fixes in existing modules — no new files except tests. The async-session fixes follow the proven in-repo pattern (`AsyncSessionLocal()` per concurrent coroutine, as in `app/routes/mining.py:284` and `app/esi/market.py:63`). Killmail-scan fixes follow the T-040 lesson: never scan unbounded ranges of the ~60M-row killmails table.

**Tech Stack:** FastAPI, SQLAlchemy async + SQLite, Jinja2/htmx, pytest (tests/ exists, run with `python3 -m pytest tests/ -v`).

**Model tiering:** Tasks 1–4 = Opus 4.8 (async/DB correctness). Tasks 5–7 = Sonnet (mechanical). Task 8 = coordinator deploy gate.

**Deploy note:** All tasks are code-only; ONE deploy at the end (Task 8) via commit→push→`ssh thunderborn-home "/opt/vigilant/scripts/deploy.sh"`. Mandatory pre-deploy checklist from CLAUDE.md applies. Task 5 touches `app/auth/routes.py` (logging lines only — flag in the checklist review, no auth behavior change).

**Security findings 0.7 from the roadmap are DROPPED** — verified already fix-in-progress on main (nginx/ deleted, notifications.js uses `esc()`); the next sec-toolkit /audit will confirm.

---

### Task 1: Per-field sessions in `_sync_fields` gather (BUG-1)

**Goal:** Each concurrently-gathered field fetcher gets its own `AsyncSessionLocal()` session and its own Character row, eliminating concurrent execute/commit on one AsyncSession.

**Files:**
- Modify: `app/routes/dashboard.py:1305-1310`
- Test: `tests/test_sync_field_sessions.py` (create)

**Acceptance Criteria:**
- [ ] No coroutine inside the gather receives the outer `db` session or the outer `char` instance
- [ ] Each fetcher gets a Character loaded in its own session (PK lookup by `character_id`)
- [ ] Verified that token refresh inside fetchers goes through `get_client_safe()` (memory: concurrent token refresh) — if any fetcher refreshes directly, note it in the report (do NOT refactor it in this task)
- [ ] Result-processing loop after the gather is unchanged (it runs sequentially on the outer `db` — that is safe)
- [ ] New regression test passes; full suite passes

**Verify:** `python3 -m pytest tests/ -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing regression test**

`tests/test_sync_field_sessions.py` — monkeypatch two fake field fetchers that capture the session object they receive; run `_sync_fields` with both fields stale; assert the two captured sessions are distinct objects and neither is the outer session. Sketch (adapt fixture setup to what `_sync_fields` actually needs — `char`, `cache`, `asset_cache` can be lightweight fakes or ORM rows in an in-memory sqlite+aiosqlite engine with `Base.metadata.create_all`; see `tests/test_killfeed_search_index_hint.py` for the engine pattern):

```python
import asyncio
import pytest
from app.routes import dashboard as dash

@pytest.mark.asyncio
async def test_field_fetchers_get_distinct_sessions(monkeypatch, ...):
    seen_sessions = []

    async def fake_fetcher(chars, db):
        seen_sessions.append(db)
        await asyncio.sleep(0)
        return {chars[0].character_id: (None, None)}

    monkeypatch.setattr(dash, "_FIELD_FETCHERS", {"wallet": fake_fetcher, "pi": fake_fetcher})
    # force both fields stale via cache.field_synced_json = None, scopes covering both
    await dash._sync_fields(character_id, char, cache, asset_cache, outer_db)
    assert len(seen_sessions) == 2
    assert seen_sessions[0] is not seen_sessions[1]
    assert outer_db not in seen_sessions
```

Also monkeypatch `dash.FIELD_CACHE_SECONDS`/`dash.FIELD_SCOPES` to only contain the two fake fields so no real fetchers run. If `AsyncSessionLocal` points at the prod DB path in tests, monkeypatch `dash.AsyncSessionLocal` to the test sessionmaker.

- [ ] **Step 2: Run test — expect FAIL** (both fetchers currently receive the same outer `db`)

Run: `python3 -m pytest tests/test_sync_field_sessions.py -v` → FAIL on distinctness assert

- [ ] **Step 3: Implement the fix** in `app/routes/dashboard.py` (replace lines 1305–1310):

```python
    if stale_fields:
        async def _run_fetcher(field: str):
            async with AsyncSessionLocal() as fdb:
                res = await fdb.execute(
                    select(Character).where(Character.character_id == character_id)
                )
                fchar = res.scalar_one()
                return await _FIELD_FETCHERS[field]([fchar], fdb)

        results = await asyncio.gather(
            *[_run_fetcher(field) for field in stale_fields],
            return_exceptions=True,
        )
```

Check imports at top of dashboard.py: `AsyncSessionLocal` and `Character` — copy the import style from `app/routes/mining.py` if missing. Read the surrounding function first: if the later result-processing loop uses `char` (the outer instance), that is fine — do not change it.

- [ ] **Step 4: Run tests** — `python3 -m pytest tests/ -v` → all PASS

- [ ] **Step 5: Syntax check + commit**

```bash
python3 -c "import ast; ast.parse(open('app/routes/dashboard.py').read())"
git add app/routes/dashboard.py tests/test_sync_field_sessions.py
git commit -m "fix(sync): per-field AsyncSessionLocal in _sync_fields gather (BUG-1)"
```

---

### Task 2: Per-coroutine sessions + semaphore in structure resolution (BUG-2)

**Goal:** `_fetch_structure` coroutines stop sharing the request session; fan-out to `/universe/structures/` is capped.

**Files:**
- Modify: `app/routes/dashboard.py:994-1007`
- Test: extend `tests/test_sync_field_sessions.py` or inline reasoning (see Step 1)

**Acceptance Criteria:**
- [ ] Each `_fetch_structure` coroutine opens its own `AsyncSessionLocal()` and passes it to `esi_universe.get_structure(..., db=...)` and `get_cached_structure`
- [ ] `asyncio.Semaphore(5)` bounds concurrency
- [ ] **Investigate `ESIClient`'s bound `db`**: the shared `client` object was constructed with `db=db` — read `app/esi/client.py` and determine whether concurrent `client.get()` calls write through that bound session (e.g. `cache_set`). If yes, that is the same bug one layer down: fix by whatever is cheapest and safe (e.g. construct the client used in this fan-out without `db`, or pass the per-coroutine session if the API allows). Document what you found in the task report.
- [ ] Full suite passes

**Verify:** `python3 -m pytest tests/ -v` → all pass

**Steps:**

- [ ] **Step 1: Read `app/esi/client.py`** — trace what `db=` is used for in `get`/`get_public` (cache reads/writes?). Decide the client handling per acceptance criterion 3.

- [ ] **Step 2: Implement** (replace dashboard.py:994-1007):

```python
    if structure_ids:
        _struct_sem = asyncio.Semaphore(5)

        async def _fetch_structure(struct_id):
            async with _struct_sem:
                async with AsyncSessionLocal() as sdb:
                    try:
                        data = await esi_universe.get_structure(client, struct_id, db=sdb)
                        sys_id = data.get("solar_system_id")
                        name = data.get("name", "Unknown Structure")
                        return struct_id, {"system_id": sys_id, "structure_name": name}
                    except Exception:
                        cached = await esi_universe.get_cached_structure(sdb, struct_id)
                        if cached:
                            return struct_id, {"system_id": cached.get("solar_system_id"), "structure_name": cached["name"]}
                        return struct_id, {"system_id": None, "structure_name": "Unknown Structure"}

        results = await asyncio.gather(*[_fetch_structure(sid) for sid in structure_ids])
```

(Adjust `client` per Step 1 findings.)

- [ ] **Step 3: Regression test** — monkeypatch `esi_universe.get_structure` with a stub capturing its `db` kwarg for 3 fake structure IDs; assert 3 distinct sessions, none the outer one. Same fixture approach as Task 1. If wiring the enclosing function is impractical (it is large), extract the structure-resolution block into a module-level helper `_resolve_structures(client, structure_ids)` first, then test that helper directly.

- [ ] **Step 4: Run tests, syntax check, commit**

```bash
python3 -m pytest tests/ -v
python3 -c "import ast; ast.parse(open('app/routes/dashboard.py').read())"
git add app/routes/dashboard.py tests/
git commit -m "fix(assets): per-coroutine sessions + semaphore for structure name resolution (BUG-2)"
```

---

### Task 3: Killfeed search — enforce default time bound (BUG-4, folds into T-037)

**Goal:** A search with no lower time bound can no longer scan the whole killmails table; it silently defaults to 90 days and tells the user.

**Files:**
- Modify: `app/routes/intel_kills_search.py` (`_compile_search_where`, ~line 270-292; results template context)
- Modify: the results partial template (find via the `intel_kills_search_results` handler, `app/routes/intel_kills_search.py:799`) — add the "defaulted to 90d" note
- Test: extend `tests/test_killfeed_search_index_hint.py`

**Acceptance Criteria:**
- [ ] EXPLAIN + timing evidence from the VPS for the unbounded count path, recorded in the task report (before-fix)
- [ ] When neither `time_preset` nor `time_start` is set, `_compile_search_where` appends `Killmail.killmail_time >= now-90d`, sets `has_time_bound=True`, and sets `defaulted_time: True` in its return dict
- [ ] Both page and count statements get the `INDEXED BY` hint on a filterless search (existing gating logic — no change needed once has_time_bound is True)
- [ ] Results UI shows a small note ("No time filter — showing last 90 days") when `defaulted_time` is set
- [ ] Existing hint tests still pass; new test covers the filterless default

**Verify:** `python3 -m pytest tests/test_killfeed_search_index_hint.py -v` → all pass

**Steps:**

- [ ] **Step 1: Gather before-fix evidence on the VPS** (read-only):

```bash
ssh thunderborn-home "docker exec vigilant-app-1 python3 - <<'EOF'
import sqlite3, time
db = sqlite3.connect('/data/vigilant.db')
q = 'SELECT count(killmail_id), sum(total_value) FROM killmails'
print(db.execute('EXPLAIN QUERY PLAN ' + q).fetchall())
t = time.time(); db.execute(q).fetchone(); print('%.1fs' % (time.time()-t))
EOF"
```

(Adapt the DB path if wrong — check `docker inspect` or app config. If the query takes >30s, Ctrl-C mentally and record "way too slow" — do not rerun it.)

- [ ] **Step 2: Write the failing test** in `tests/test_killfeed_search_index_hint.py`:

```python
def test_filterless_search_gets_default_time_bound_and_hint():
    compiled = _compile({"sort": "date", "direction": "desc"})
    assert compiled["has_time_bound"] is True
    assert compiled.get("defaulted_time") is True
    stmt, count_stmt = _build_search_statements(compiled, live=0, since=0)
    assert f"INDEXED BY {KILLMAIL_TIME_INDEX}" in _sql(stmt)
    assert f"INDEXED BY {KILLMAIL_TIME_INDEX}" in _sql(count_stmt)
    assert "killmail_time >=" in _sql(count_stmt)
```

Run → FAIL.

- [ ] **Step 3: Implement** in `_compile_search_where`, after the time_end block (~line 292):

```python
    # Safety net: without a lower time bound every search (page AND count)
    # scans the whole ~60M-row table. Default to 90 days and surface it.
    defaulted_time = False
    if not has_time_bound:
        where.append(Killmail.killmail_time >= datetime.utcnow() - timedelta(days=90))
        has_time_bound = True
        defaulted_time = True
```

and include `"defaulted_time": defaulted_time` in the returned dict. Then in `intel_kills_search_results`, pass `defaulted_time` into the template context and render the note in the results partial header (match existing `.kf-*` styling; keep it one muted line).

- [ ] **Step 4: Run tests, syntax check, commit; note T-037 in the message**

```bash
python3 -m pytest tests/ -v
python3 -c "import ast; ast.parse(open('app/routes/intel_kills_search.py').read())"
git add app/routes/intel_kills_search.py app/templates/ tests/test_killfeed_search_index_hint.py
git commit -m "fix(killfeed): default 90d time bound on filterless search (T-037 / BUG-4)"
```

---

### Task 4: Bound `streaks()` history scan (BUG-3)

**Goal:** `streaks()` stops loading a character's entire all-time kill history; it reads at most the 20,000 most recent involvements.

**Files:**
- Modify: `app/intel/kill_queries.py:371-410`
- Test: `tests/test_kill_streaks.py` (create)

**Acceptance Criteria:**
- [ ] Query orders `killmail_time DESC` with `LIMIT 20000`, rows reversed in Python before the existing streak walk (which requires ascending order)
- [ ] Docstring notes the approximation: streaks are exact within the last 20k involvements; `longest_win` older than that is not counted
- [ ] Unit test seeds an in-memory DB (engine pattern from `tests/test_killfeed_search_index_hint.py`, `Base.metadata.create_all`) with a known kill/loss sequence and asserts `current_win`, `longest_win`, `days_since_loss` are unchanged from the naive implementation
- [ ] Compiled SQL contains `LIMIT` (assert via `str(q.compile(...))` or seed >LIMIT rows in a smaller-limit test via monkeypatched constant — prefer making the limit a module constant `STREAKS_MAX_ROWS = 20000` so the test can patch it)

**Verify:** `python3 -m pytest tests/test_kill_streaks.py -v` → pass

**Steps:**

- [ ] **Step 1: Write the test** — seed ~10 killmails (mix of victim/attacker rows for a char, use `Killmail` + `KillmailAttacker` models), compute expected streaks by hand, assert. Add a second test: with `STREAKS_MAX_ROWS` monkeypatched to 3, only the 3 most recent rows influence the result.

- [ ] **Step 2: Implement**:

```python
STREAKS_MAX_ROWS = 20_000  # module level

async def streaks(character_id: int) -> dict:
    """Current win streak, longest win streak, days since last loss.
    Exact within the character's STREAKS_MAX_ROWS most recent involvements;
    history older than that is ignored (unbounded scan OOMs on the 60M-row
    table — see 2026-07-04 audit BUG-3)."""
    async with AsyncSessionLocal() as db:
        attacker_ids_q = select(KillmailAttacker.killmail_id).where(
            KillmailAttacker.character_id == character_id
        )
        q = (
            select(Killmail.killmail_time, Killmail.victim_character_id)
            .where(or_(
                Killmail.victim_character_id == character_id,
                Killmail.killmail_id.in_(attacker_ids_q),
            ))
            .order_by(Killmail.killmail_time.desc())
            .limit(STREAKS_MAX_ROWS)
        )
        rows = list(reversed((await db.execute(q)).all()))
```

(rest of the function unchanged)

- [ ] **Step 3: Timing sanity on VPS** (after Task 8's deploy, part of its verification): character-detail page for the busiest character; compare `/data/logs/perf.log` span. Record numbers in report.

- [ ] **Step 4: Run tests, syntax check, commit**

```bash
python3 -m pytest tests/ -v
python3 -c "import ast; ast.parse(open('app/intel/kill_queries.py').read())"
git add app/intel/kill_queries.py tests/test_kill_streaks.py
git commit -m "fix(intel): bound streaks() to 20k most recent involvements (BUG-3)"
```

---

### Task 5: Log swallowed exceptions (BUG-5 + auth enrichment) — Sonnet

**Goal:** Silent failures become log lines; zero behavior change.

**Files:**
- Modify: `app/routes/dashboard.py:2963-2988` (corp-stats `fetch_corp_data`)
- Modify: `app/auth/routes.py:144-163` (birthday/corp/alliance enrichment)

**Acceptance Criteria:**
- [ ] dashboard corp-stats: outer `except Exception` logs `logger.warning("corp-stats fetch failed for corp %s: %s", corp_id, exc)`; the per-result skip at line 2966 logs exceptions it discards at warning level with the label
- [ ] auth enrichment: the three `except Exception: pass` blocks log at debug level (`logger.debug("corp name lookup failed for %s: %s", corporation_id, exc)` etc.) — confirm `logger` exists in the module, add `logger = logging.getLogger(__name__)` if not
- [ ] No functional change: same fallbacks, same control flow; suite passes

**Verify:** `python3 -m pytest tests/ -v` and `python3 -c "import ast; ast.parse(open('app/routes/dashboard.py').read()); import ast as a; a.parse(open('app/auth/routes.py').read())"`

**Steps:**

- [ ] **Step 1:** dashboard.py — change `except Exception: pass` (2987-2988) to capture `as exc` and warn; in the zip loop change `if result is None or isinstance(result, Exception): continue` to log first when `isinstance(result, Exception)`.
- [ ] **Step 2:** auth/routes.py — add `as exc` + `logger.debug(...)` to the three blocks (birthday parse :147, corp :156, alliance :162).
- [ ] **Step 3:** Syntax check both files, run suite, commit:

```bash
git add app/routes/dashboard.py app/auth/routes.py
git commit -m "chore(obs): log previously-swallowed corp-stats and auth enrichment errors (BUG-5)"
```

---

### Task 6: Semaphore on fittings ship-info fan-out (BUG-6) — Sonnet

**Goal:** Cap the per-hull `_get_ship_info` gather at 5 concurrent to respect the ESI fan-out rule.

**Files:**
- Modify: `app/routes/fittings.py:175-178`

**Acceptance Criteria:**
- [ ] `asyncio.Semaphore(5)` wraps each `_get_ship_info` call; result mapping by ship id unchanged
- [ ] First check `_get_ship_info` (same file) — if it is already served from a DB/type cache before hitting ESI, note that in the report and STILL add the semaphore (cheap insurance)
- [ ] Suite passes

**Verify:** `python3 -m pytest tests/ -v`

**Steps:**

- [ ] **Step 1: Implement** (replace fittings.py:176-177):

```python
        _ship_sem = asyncio.Semaphore(5)

        async def _sem_ship_info(sid):
            async with _ship_sem:
                return await _get_ship_info(client, sid)

        ship_info_tasks = {sid: _sem_ship_info(sid) for sid in ship_type_ids}
        ship_results = await asyncio.gather(*ship_info_tasks.values())
```

- [ ] **Step 2:** Syntax check, suite, commit:

```bash
python3 -c "import ast; ast.parse(open('app/routes/fittings.py').read())"
git add app/routes/fittings.py
git commit -m "fix(fittings): semaphore(5) on per-hull ship-info ESI fan-out (BUG-6)"
```

---

### Task 7: Migrate status pages to the design system (folds into T-039) — Sonnet

**Goal:** `status.html` and `status_data.html` stop using legacy Tailwind `eve-*` classes and match the site-wide `b-*` design system, including a proper page header.

**Files:**
- Modify: `app/templates/status.html`
- Modify: `app/templates/status_data.html`

**Acceptance Criteria:**
- [ ] Zero `eve-` class references remain in either template (`grep -n "eve-" app/templates/status*.html` → empty)
- [ ] Zero raw Tailwind utility stacks remain (`space-y-`, `flex items-center`, `text-sm`, etc.)
- [ ] Both pages use `.b-page-header` + `.b-page-title` and `.b-panel` sections — copy structure from a comparable migrated page (`app/templates/admin.html` is the closest analog; also see `wormholes.html`)
- [ ] Keep the existing breadcrumb block in status.html; content/data bindings unchanged (Jinja expressions untouched except class attributes)
- [ ] Templates still render: `python3 -c "from jinja2 import Environment, FileSystemLoader; e=Environment(loader=FileSystemLoader('app/templates')); e.get_template('status.html'); e.get_template('status_data.html')"` (syntax-level check)

**Verify:** grep + Jinja parse commands above; visual check happens post-deploy in Task 8

**Steps:**

- [ ] **Step 1:** Read both templates fully and `admin.html` for the target idiom. List each legacy class → `b-*` mapping before editing.
- [ ] **Step 2:** Rewrite class attributes; add `.b-page-header` with title "Status" (and status_data equivalent). Do not restructure the data content.
- [ ] **Step 3:** Run the grep + Jinja parse checks, commit:

```bash
git add app/templates/status.html app/templates/status_data.html
git commit -m "style(status): migrate status pages to b-* design system (T-039)"
```

---

### Task 8: Deploy + verify (coordinator gate)

**Goal:** Ship all Phase 0 commits in one deploy, run the mandatory pre-deploy checklist, and verify each fix in production.

**Files:** none (ops)

**Acceptance Criteria:**
- [ ] Pre-deploy checklist: `ast.parse` on every modified .py; changed-file review notes `app/auth/routes.py` touched (logging only); no DB schema changes (none in this phase); full pytest suite green
- [ ] Push BEFORE deploy (deploy.sh pulls from GitHub — deploying unpushed commits silently runs old code)
- [ ] `docker logs vigilant-app-1` clean after deploy; app serving
- [ ] Spot checks: `/dashboard` loads and sync completes without greenlet/InvalidRequestError in logs; character-detail for the busiest character (streaks timing vs `/data/logs/perf.log` baseline); `/intel/kills/search` filterless search returns fast with the 90d note; `/status` renders in design-system styling
- [ ] Update `.story/tickets/T-037.json` (add note: default time bound shipped) and T-039 (status pages done) — status stays open if other subtasks remain
- [ ] Refresh `SECURITY_TODO.md`: `~/sec-toolkit/bin/sec_findings.py open-md vigilant-vps > SECURITY_TODO.md`

**Verify:** `ssh thunderborn-home "docker logs vigilant-app-1 --since 5m 2>&1 | grep -iE 'error|traceback' | head"` → empty (or only known-benign lines)

**Steps:**

- [ ] **Step 1:** `python3 -m pytest tests/ -v` → all green; ast-parse every changed .py
- [ ] **Step 2:** `git push origin main`
- [ ] **Step 3:** `ssh thunderborn-home "/opt/vigilant/scripts/deploy.sh"`
- [ ] **Step 4:** Log check + the four spot checks above; record streaks timing and killfeed-search timing in the session report
- [ ] **Step 5:** Ticket updates + SECURITY_TODO refresh; commit ticket JSON changes

```bash
git add .story/tickets/T-037.json .story/tickets/T-039.json
git commit -m "chore: sync T-037/T-039 ticket notes after Phase 0 deploy"
git push origin main
```
