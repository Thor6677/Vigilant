# Vigilant Design System — Design Spec

**Date:** 2026-07-02
**Status:** Approved by user (brainstorm session with visual companion)
**Scope:** Sub-project 1 of 2 — the design system package itself. Sub-project 2 (rolling the new look onto the live Jinja2 site) gets its own spec later.

## Goal

Create a formal design system for Vigilant that:

1. Codifies and modernizes the existing brutalist look (dark `#080808`, JetBrains Mono, gold `#c8a951`, zero border-radius) into an **expressive** variant: glass/transparent surfaces, gold corner brackets, animated menus, motion throughout.
2. Ships as a React component library (`@vigilant/ui`) that `/design-sync` can upload to claude.ai/design, so the design agent prototypes new Vigilant pages from real components.
3. Uses **one CSS source of truth** shared by both the future Jinja2 site rollout and the React library (Approach A from brainstorm).

## Decisions made during brainstorm

| Decision | Choice |
|---|---|
| Primary purpose | Feed claude.ai/design via /design-sync (React library, package shape, no Storybook) |
| Visual direction | Refined brutalist base, **expressive** motion tier: glass-blur menus with light sweeps, glowing text accents, corner brackets on panels, shine sweep on primary buttons, staggered/terminal-style row entrances |
| Transparency | Prefer translucent glass surfaces (`rgba` + `backdrop-filter: blur`) wherever content sits over the background |
| Background | Ambient "flying through New Eden" canvas: real SDE system coordinates (5,485 k-space systems from `frontend/public/data/systems.json`), no jump lines, camera on a slow elliptical cruise through the disc, depth fog + soft star glow. **Scope amended at Task 10 review: ambient background is for the SSO login screen only; logged-in pages use the plain dark background.** |
| Background colors | **Live sovereignty colors**: empire faction colors (Amarr gold, Caldari steel blue, Gallente green, Minmatar rust) from ESI `/sovereignty/map/` (public, no auth); nullsec alliances get stable distinct colors via golden-angle hue on alliance ID; unclaimed space neutral starlight |
| Kill activity | Red expanding blips on real systems as kills occur; camera never focuses on them. Pluggable event source: production endpoint later, simulation fallback for previews. Small ticker optional. |
| Site restyle | In scope overall ("both together") but decomposed: this spec covers the library; site rollout is sub-project 2 |
| Class names | Keep the existing `b-*` vocabulary so the site rollout is mostly a stylesheet swap |

## Architecture

```
design-system/
├── css/
│   ├── tokens.css        # design tokens (CSS custom properties)
│   ├── components.css    # b-* component vocabulary, restyled + new components
│   └── motion.css        # keyframes, transitions, prefers-reduced-motion guards
├── ambient/
│   └── vigilant-ambient.js   # map-flythrough background module (dependency-free)
└── react/                # @vigilant/ui — the design-sync artifact
    ├── package.json
    ├── src/
    └── dist/             # esbuild output: ESM + .d.ts + bundled CSS
```

- React components render the same `b-*` classes the Jinja site uses; the package imports the shared CSS so it is fully self-contained for design-sync.
- The Jinja2 site (sub-project 2) will link the same three stylesheets from `/static/`.

## Token layer (`tokens.css`)

Extends the current palette; does not replace it.

- **Keep:** `--bg #080808`, `--surface`, `--text #dedede`, `--muted #8a8a8a`, `--border`, `--rule`, `--accent #c8a951`, `--danger`, `--success`, `--warn`; JetBrains Mono.
- **Add:**
  - Glass: `--glass-bg: rgba(13,13,17,.55)`, `--glass-border: rgba(200,169,81,.35)`, `--glass-blur: 8px`, glow shadow tokens.
  - Surface elevation: 3-step scale (base / raised / overlay).
  - Motion: `--dur-fast: 180ms`, `--dur-menu: 280ms`, `--ease-pop: cubic-bezier(.2,.9,.25,1.15)`, standard ease tokens.
  - Semantic: `--info` plus existing danger/success/warn.
  - Typography + spacing + letter-spacing scales, z-index layers.
- Sec-status and sov colors live in the ambient module, **not** the token layer.

## Component CSS (`components.css`)

Restyle the existing ~70 `b-*` classes to the expressive look, keeping names. Signature treatments:

- Glass panels with gold corner brackets (`::before`/`::after`).
- Dropdown menus: glass blur, gold top rule, scale+slide entrance with light sweep, links that indent/brighten on hover.
- Primary buttons: gold fill, periodic shine sweep, hover glow + lift. Ghost and danger variants.
- Tables/rows: staggered fade-up entrance, hover row highlight.
- New components the site lacks today: modal, toast, tooltip, skeleton loader, switch/checkbox, styled text inputs/selects, search input.

All animation lives in `motion.css` and is fully disabled under `prefers-reduced-motion`.

## Ambient background module (`vigilant-ambient.js`)

Dependency-free ES module + IIFE build, rendering to a fixed full-viewport canvas behind content.

- **Data:** system coordinates JSON (same shape as `frontend/public/data/systems.json`), URL configurable.
- **Sov colors:** fetch ESI `/sovereignty/map/` on load; cache in localStorage with 24h TTL; graceful fallback to neutral starlight if unreachable.
- **Kills:** `killSource` config — `{ type: 'endpoint', url }` (poll/SSE, production) or `{ type: 'simulate' }` (previews/demo). Blip = expanding red ring + brief core flash on the real system.
- **Performance:** ~30fps cap, pause on `document.hidden`, disabled under `prefers-reduced-motion` and below a viewport-width threshold (configurable, default 768px). Canvas 2D, no WebGL dependency.
- **API:** `VigilantAmbient.mount(el, options)` / `.destroy()`; React wrapper `<AmbientBackground>`.

## React library (`@vigilant/ui`)

~22 components, thin typed wrappers over the CSS classes. Zero runtime deps beyond React.

| Group | Components |
|---|---|
| Layout | NavBar, NavMenu (animated dropdown), Breadcrumbs, PageHeader, Section, Panel, Grid, TabStrip, Footer |
| Data | StatBlock, KeyValueRow, Table/TableRow, Badge, ProgressBar, EmptyState, Eyebrow |
| Actions | Button (primary/ghost/danger), ButtonGroup |
| Feedback | Banner, Toast, Modal, Skeleton |
| Ambient | AmbientBackground |

- Each component exports `<Name>` and `<Name>Props`.
- Build: esbuild → `dist/index.js` (ESM) + `.d.ts` via tsc + CSS copied/bundled into the package.
- Fonts: JetBrains Mono woff2 files shipped in the package so previews render offline.
- A Vite demo page (`react/demo/`) renders every component with realistic EVE content (ISK stats, kill rows, fleet panels) — the dev eyeball harness and the usage-example source for design-sync prompt docs.

## Verification

- Demo page visual review during development.
- `tsc --noEmit` clean; esbuild build clean.
- Final gate: run `/design-sync` (package shape). Its own verification loop grades every component preview before upload to a new claude.ai/design project.

## Error handling

- Ambient module: all network failures degrade silently (neutral colors, no blips); never blocks page render; canvas errors caught so a broken background can't take down a page.
- React components: no runtime fetching except AmbientBackground; everything else is presentational and prop-driven.

## Out of scope (sub-project 2, separate spec)

- Replacing `base.html`'s ~1,100 inline CSS lines with links to the three stylesheets.
- Kill-events endpoint in the FastAPI app for live ambient blips.
- Page-by-page straggler sweep of the 111 templates.
- Deploy via `deploy.sh`.

## Implementation amendments (recorded at final review, 2026-07-03)

Deliberate deviations from this spec accepted during build/review; the code is authoritative:

- Ambient module ships as **ES module only** (no IIFE build). Sub-project 2 loads it via `<script type="module">`.
- No dedicated search-input class — generic `.b-input` covers search fields.
- Ambient kill source is `{type:'poll', url, intervalMs}` (plus `{type:'simulate'}`), not the spec's `{type:'endpoint'}` naming; SSE deferred until the production endpoint exists.
- Ambient background is scoped to the **SSO login screen only** (Task 10 review-gate decision; also noted in the Background decision row above).

## Reference material

Brainstorm mockups (visual companion session) are preserved at `.superpowers/brainstorm/73208-1783035198/content/` — notably `motion-flair.html` (expressive tier C), `mapfly-sov.html` (approved background with live sov colors), and `vanta-demo.html` (rejected Vanta.js exploration).
