# Site Restyle Rollout (Design System Sub-Project 2) — Design Spec

**Date:** 2026-07-03
**Status:** Approved by user
**Predecessor:** `docs/superpowers/specs/2026-07-02-vigilant-design-system-design.md` (sub-project 1, shipped: `design-system/` CSS core + ambient module + @vigilant/ui)

## Goal

Roll the expressive-brutalist design system onto the live Jinja2 site: swap `base.html`'s inline CSS for the shared stylesheets, rebuild the SSO login page around the ambient New Eden flythrough, feed it live kill data, then sweep all 111 templates.

## Decisions

| Decision | Choice |
|---|---|
| Rollout | **Big-bang deploy** — one deploy swaps the look site-wide; `deploy.sh`'s `:prev` tag is the rollback |
| Sweep | **Full sweep** — every one of the 111 templates verified against a checklist |
| Ambient scope | **SSO login page only** (`index.html`), per the sub-project-1 Task 10 review decision |
| Kill blips | **Real kills** — polled from the local `Killmail` table, not simulated |

## 1. Stylesheet swap

- New mount in `app/main.py`: `app.mount("/static/ds", StaticFiles(directory="design-system"), name="static-ds")`. The Docker image already contains the repo, so `design-system/css/*` and `design-system/ambient/*` ship with every build — one source of truth, no copy step.
- `base.html` `<head>` replaces the shared-vocabulary portion of its inline `<style>` with:
  ```html
  <link rel="stylesheet" href="/static/ds/css/tokens.css">
  <link rel="stylesheet" href="/static/ds/css/motion.css">
  <link rel="stylesheet" href="/static/ds/css/components.css">
  <link rel="stylesheet" href="/static/css/site.css">
  ```
  (External same-origin CSS is already CSP-permitted — `tailwind.css` is linked today. JetBrains Mono keeps loading from Google Fonts as now; the design-system `fonts/` dir is for the claude.ai/design bundle, not the site.)
- **`static/css/site.css` (new)** holds everything `base.html` has that the design system deliberately does not cover, moved verbatim (with the fixes below): focus-visible rules, mobile hamburger + `b-mobile-menu`, responsive `@media` blocks, notification bell/badge/dropdown styles, `structure-alert-banner`, `b-server-bar` page-specific tweaks if any, `htmx-indicator`, `#esi-banner` override, `.corp-logo`, and any remaining one-off utility. It loads AFTER the design-system sheets so its responsive overrides win the cascade.
- Folded fixes (from sub-project-1 reviews):
  - notif-dropdown inline `z-index:1000` → `var(--z-dropdown)` (60; above nav 50, below modal 100)
  - `body { margin: 0 }` arrives with components.css — the 8px shift is accepted (demo layouts assume it)
  - Legacy `spin`/`pulse` keyframes come from motion.css; delete the base.html copies
- What must NOT break (verify explicitly): dismissed-banner re-apply on `htmx:afterSwap`, notif dropdown behavior, mobile menu, `b-*` markup across templates (class names unchanged by design).

## 2. Login page (`index.html`)

Rebuilt to the demo's approved login view (see `design-system/react/demo/main.tsx` login branch):

- Ambient canvas behind a centered glass panel (`b-panel is-glass is-brackets` idiom in plain HTML/Jinja): logo, "EVE ONLINE COMPANION DASHBOARD" eyebrow, short feature rows, gold primary CTA `LOG IN WITH EVE ONLINE SSO` (`b-btn is-primary`), existing `error` banner slot restyled as `b-banner is-danger`, "SSO · CCP authorized third-party" footnote.
- Ambient mount:
  ```html
  <script type="module" nonce="{{ request.state.csp_nonce }}">
    import { mount } from '/static/ds/ambient/vigilant-ambient.js';
    mount(document.body, {
      systemsUrl: '/api/map/kspace-data/systems.json',
      killSource: { type: 'poll', url: '/api/ambient/kills', intervalMs: 15000 }
    });
  </script>
  ```
- Mounted on `document.body` (never inside a glass/filtered ancestor — containing-block trap). Module degrades silently on any failure and no-ops under reduced motion / narrow viewports; login never blocks on it.
- Tailwind classes on this page are replaced by `b-*` + tokens (the page currently mixes `text-eve-*` utility classes).
- No other template mounts the ambient module.

## 3. Public data endpoints

The login page is pre-auth, so both ambient data sources must be publicly reachable. Both are public game data (SDE coordinates; kill locations already public on zKillboard).

- **Systems:** `/api/map/kspace-data/systems.json` (existing route, runtime SDE data with `frontend/dist/data` fallback). Confirm it is reachable without a session; if auth-gated, exempt exactly this read-only endpoint.
- **Kills (new):** `GET /api/ambient/kills` in a small new route module:
  - Query: distinct `solar_system_id` from `Killmail` where `killmail_time >= now − 120s`, LIMIT 50, using the existing `ix_killmail_system_time` index.
  - Response: `[{"system_id": 30000142}, …]` — no names, values, or IDs beyond the system. `Cache-Control: public, max-age=15`.
  - No auth. No DB writes. Failure modes return `[]` (the module treats any non-OK as no blips).

## 4. Rollout, sweep, verification

- **Pre-deploy** (CLAUDE.md checklist): `python3 -c "import ast; …"` on changed `.py`; explicit flag to the user that two endpoints become public (auth-surface review); no DB schema changes.
- **Deploy:** commit → push → `ssh thunderborn-home "/opt/vigilant/scripts/deploy.sh"`. Verify startup via `docker logs vigilant-app-1`, then smoke: login page (ambient + SSO button), dashboard, one htmx-heavy page. If broken: `rollback.sh` then `git revert`.
- **Full sweep:** checklist of all 111 templates grouped by nav section (Dashboard, Intel, Industry, Assets, Corp, Tools, Admin, partials). Each page: renders, no washed-out/unstyled regions, dropdowns/menus work, page-local inline styles don't clash. Fixes land in batches with redeploys.
- **Tests:** pytest for `/api/ambient/kills` (empty table → `[]`; recent kill → included; stale kill → excluded; cache header present). Template smoke where the existing test setup allows.

## Error handling

- Ambient: all network failures silent; canvas errors cannot block login (module contract from sub-project 1).
- Kill endpoint: DB error → empty list + 200 (background decoration must never 500 the login page path).
- CSS swap regression risk is carried by the rollback path, not by feature flags (explicit decision).

## Out of scope

- Restyling the React star map app (`/map` keeps its own look).
- Replacing Tailwind on pages other than `index.html` (sweep fixes clashes; wholesale Tailwind removal is future work).
- SSE kill streaming (poll is sufficient at 15s; matches the module's `{type:'poll'}` support).

## Known gotchas carried in

- backdrop-filter ancestors are containing blocks for `position:fixed` (ambient/canvas + notif dropdown placement)
- htmx swaps re-run nothing automatically — the swap must not move scripts out of `{% block content %}`
- Dismissed-banner state re-applies via the global `htmx:afterSwap` handler in base.html — keep that script intact
- The repo's `~/Documents` file-sync can create `" 2"` conflict-copy dirs during heavy writes — check trees before commits
