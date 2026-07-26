# Contributing to Vigilant

Thanks for taking an interest. Vigilant is a self-hosted EVE Online companion dashboard — a FastAPI + Jinja2/htmx backend with a React/Pixi.js star map. Bug reports, feature PRs, and SDE/dogma accuracy fixes are all welcome.

---

## Getting a dev environment running

### 1. Your own EVE developer application

Vigilant authenticates entirely through EVE SSO, so you need your **own** application — there is no shared dev app.

1. Go to [developers.eveonline.com](https://developers.eveonline.com/) and create an application
2. **Connection Type**: Authentication & API Access
3. **Scopes**: select the ones listed under [ESI Scopes](README.md#esi-scopes) in the README
4. **Callback URL**: `http://localhost:8000/auth/callback`
5. Save the **Client ID** and **Client Secret**

### 2. Start the app

```bash
git clone https://github.com/Thor6677/Vigilant.git
cd Vigilant
./start.sh
```

`start.sh` creates `.env` from `.env.example`, generates a `SECRET_KEY`, prompts for your Client ID/Secret, builds `.venv`, installs `requirements.txt`, and launches uvicorn on port 8000. Logs go to `vigilant.log`; `./stop.sh` shuts it down.

- **Python 3.12+** is required.
- **`.env` is gitignored and must stay that way.** Never commit real credentials, and don't paste them into issues or PRs.
- The star map at `/map` needs the frontend built: `cd frontend && npm ci && npm run build` (Node 22+). The Docker image does this for you.
- Docker path, if you prefer it: `docker compose up -d --build`. Code changes require a **rebuild** — `docker compose restart` alone does not pick them up.

---

## Running the tests

`start.sh` only installs runtime deps, so install the dev deps once:

```bash
source .venv/bin/activate
pip install -r requirements-dev.txt   # pytest, aiosqlite, greenlet
```

Then, **from the repo root**:

```bash
pytest tests -q
```

Around 290 tests, roughly 9 seconds. Run it from the root — there is no `pytest.ini` or `pyproject.toml`, so pytest's rootdir handling is what makes `import app.main` resolve.

There is **no Python linter, formatter, or type-checker** in this repo. Don't run (or add) black/ruff/mypy as part of a PR; match the style of the file you're editing instead. The frontend does have ESLint: `cd frontend && npm run lint`, and `npm run build` type-checks via `tsc -b`.

---

## New behavior should come with a test

The suite is fast and cheap on purpose — please extend it. Some real examples to model:

- **`tests/test_invention_math.py`** — pure-function math against known reference values. This is the easiest and most valuable shape: import the calculation, assert `pytest.approx` against a number you can defend, cover the clamps and floors.
- **`tests/test_nav_registry.py`** — the dead-link guard. It imports the real FastAPI app and asserts every internal URL in `app/nav.py`'s `NAV_GROUPS` resolves to a registered route. If you add a page, add its entry to `app/nav.py` or this test fails.
- **`tests/test_sync_field_sessions.py`** — a concurrency regression test. Note its shape: **there is no pytest-asyncio here.** Async tests manage a single event loop by hand (`asyncio.new_event_loop()` / `run_until_complete`). It also uses a temp *file* SQLite DB rather than `:memory:`, because `:memory:` collapses to one shared pooled connection and can't model real concurrent sessions.

`tests/conftest.py` keeps the suite hermetic: it sets dummy `EVE_CLIENT_ID`, `EVE_CLIENT_SECRET`, and `SECRET_KEY` values so `app.main` imports cleanly, points `DATABASE_URL` at a throwaway temp SQLite file, and creates the schema there before any test runs. That last part matters — the route smoke tests drive a `TestClient` against the real app, and without it they would read and write your own `./vigilant.db`. **Never point the suite at a database you care about**, and don't make real network calls from a test.

---

## Conventions worth knowing before you start

These are the ones that bite newcomers hardest. They're all load-bearing — each represents a bug someone already shipped:

- **Jinja2 dict access** — use `dict['key']`, not `dict.key`, for any key that collides with a dict method (`items`, `keys`, `values`, …). Dot notation silently returns the bound method, not your value.
- **Prefer htmx over `fetch()`** for dynamic content. htmx is initialized globally in `base.html`; use `hx-get` with `hx-trigger="load"`. Hand-rolled `fetch()` inside IIFEs fails silently and is miserable to debug.
- **One `AsyncSession` per coroutine.** Never share a session across `asyncio.gather` coroutines that write — SQLAlchemy raises greenlet/`InvalidRequestError` non-deterministically. Each concurrent coroutine gets its own `AsyncSessionLocal()`. See `tests/test_sync_field_sessions.py`.
- **Jinja2 script blocks** must live *inside* `{% block content %}`. Anything after `{% endblock %}` is discarded, and htmx-loaded partials can redefine functions from the parent page.
- **Detached SQLAlchemy instances** — extract model fields into a plain dict before handing them to a template after an await, or you'll hit lazy-load errors.
- **Corp ESI endpoints need a fallback** — a given character may lack the in-game Director role and 403. Use `_try_api_call_with_fallback()` and cycle through characters holding the scope.
- **Database changes** — new columns need a default or an `ALTER TABLE` in the migration block in `app/main.py`. New tables are created automatically by `Base.metadata.create_all`.

---

## Pull requests

1. Fork, then branch off `main` (`git checkout -b fix/gatecheck-route-parsing`)
2. Keep commits small and focused; one logical change per commit
3. Run `pytest tests -q` and make sure it's green
4. In the PR description, say what changed, why, and how you verified it. Screenshots help a lot for UI work.
5. Don't reformat unrelated code, and don't commit `.env`, `vigilant.db`, `vigilant.log`, or build output

Opening an issue first is a good idea for anything large or architectural — it's cheaper than finding out after you've written it.

Security issues do **not** belong in a public PR or issue. See [SECURITY.md](SECURITY.md).

---

By contributing, you agree your changes ship under the project's [MIT license](LICENSE).
