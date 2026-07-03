# design-sync notes — Vigilant

- **Dark-first DS**: preview card chrome is white, but Vigilant assumes page bg `#080808`. Every preview is wrapped via `cfg.provider` → `DarkSurface` (exported from `design-system/react/preview-surface.mjs` via `extraEntries`). Without it, ghost/glass surfaces and `--text #dedede` wash out to invisible. Never remove the provider.
- **Entry/build**: converter runs from repo root with `--node-modules design-system/react/node_modules --entry design-system/react/dist/index.js`. `dist/` is gitignored — always run `cfg.buildCmd` first on a fresh clone.
- **AmbientBackground's preview mounts the real component with an honest "live canvas" description panel** — it fetches systems.json + ESI sov at runtime, so the static capture shows the label, not the flythrough. Don't replace with a faked starfield.
- **Modal/ToastStack render via React portals** to document.body (position:fixed); they use `cardMode: single` overrides so the open state renders inside their card.
- NavMenu's dropdown is CSS `:hover`/`:focus-within` — static previews show the closed state only.
- **Config edits invalidate the stamped manifest**: adding `overrides` after a full build makes every scoped `preview-rebuild` on affected components fail `[CONFIG_STALE]` — orchestrator must re-run `package-build.mjs` before subagents can touch them (bit the feedback wave on Modal/ToastStack).
- Full-width components (NavBar/Footer/PageHeader/TabStrip) render fine in default cards — no `cardMode: column` needed.
- `b-stat-val` has tone rules only for `is-accent`/`is-danger`/`is-ok` — warn/muted on a bare StatBlock fall through to the global utilities; prefer the covered tones in previews.
- `b-grid-3` children get flat `--surface-1` backgrounds by design (1px seam grid).

## Known render warns
- (none — 26/26 clean as of first sync; AmbientBackground's earlier RENDER_BLANK was resolved by its authored live-canvas description preview)

## Re-sync risks
- **This repo lives in ~/Documents, which a file-sync service (iCloud-style) watches**: rebuilds occasionally get directories hijacked into `" 2"` conflict copies (`components/general 2`, `dist 2`, `index 2.css`) seconds after writes. Before ANY upload: `find ds-bundle -name "* 2*"` must be empty and the component count must be stable across a few seconds; if corrupted, `rm -rf ds-bundle` and rebuild (grades carry). Same check applies to `design-system/react/dist/` before the converter runs.
- `dist/` is gitignored: every re-sync must run `cfg.buildCmd` before the converter or `[NO_DIST]`.
- `preview-surface.mjs` (DarkSurface provider) lives in the package dir but is NOT part of the package build — if the package is ever restructured, keep this file or every preview washes out on white.
- The ambient module fetches ESI at runtime; previews never exercise it (floor card) — sub-project 2's login page is where it gets truly verified.
- Playwright chromium pinned via ~/Library/Caches/ms-playwright (installed 2026-07-03, playwright in .ds-sync/node_modules).
