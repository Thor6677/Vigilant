# Vigilant Design System

Expressive-brutalist design system for Vigilant (EVE Online companion dashboard).
Dark `#080808`, JetBrains Mono, gold `#c8a951`, zero border-radius — with glass
surfaces, gold corner brackets, animated menus, and an ambient "flying through
New Eden" background (login screen only — see below).

## Layout

- `css/` — the source of truth. `tokens.css` (custom properties), `motion.css`
  (all keyframes), `components.css` (the `b-*` vocabulary). The Jinja2 site
  consumes these directly; the React package bundles them.
- `ambient/` — `vigilant-ambient.js`, dependency-free canvas module: real New
  Eden map flythrough, live ESI sov colors, kill blips. `mount(el, opts)` →
  `{ destroy(), flare?(systemId) }`.
- `react/` — `@vigilant/ui`. Thin typed wrappers over the CSS classes.

## Styling idiom

Components are styled by `b-*` classes with `is-*` state/tone modifiers
(`is-active`, `is-danger`, `is-ok`, `is-warn`, `is-accent`, `is-muted`,
`is-glass`, `is-brackets`, `is-primary`, `is-ghost`, `is-info`). Design values
come from CSS custom properties in `tokens.css` (`var(--accent)`,
`var(--glass-bg)`, `var(--dur-menu)`, …). No utility-class framework; no
inline hex colors.

`.b-panel` ships with no default body padding — its children (or a title bar
via `b-panel-head`) render flush. Add `b-pad-md` (or your own padding) to the
content you place inside a `Panel`.

## Ambient background — login only

`AmbientBackground` / `vigilant-ambient.js` renders the New Eden flythrough
used on the **SSO login screen only**; logged-in pages use the plain dark
background (`#080808`). This was a review-gate decision — the ambient canvas
is `position:fixed`, so it's reserved for the one full-bleed, chrome-free
screen where a moving background doesn't compete with dashboard content.

## Component inventory

22 components exported from `react/src/index.ts`: `Button`, `NavBar`,
`NavMenu`, `Breadcrumbs`, `PageHeader`, `Section`, `Panel`, `Grid`,
`TabStrip`, `Footer`, `StatStrip`, `KeyValueRow`, `Table`, `Badge`,
`ProgressBar`, `EmptyState`, `Eyebrow`, `ButtonGroup`, `Banner`, `Toast`,
`Modal`, `Skeleton` — plus `AmbientBackground` (the login flythrough wrapper
above). Three modules additionally co-export a secondary component:
`StatStrip` → `StatBlock`, `Table` → `TableRow`, `Toast` → `ToastStack`.

### Consumption notes

- **`Modal` and `ToastStack` render via `createPortal(..., document.body)`.**
  This is deliberate: `.b-panel.is-glass` and other glass surfaces use
  `backdrop-filter`, which makes the element a containing block for
  `position: fixed` descendants. Without the portal, a modal or toast stack
  nested inside a glass panel would be clipped/re-anchored to that panel
  instead of the viewport.
- **`AmbientBackground` needs a non-transformed, non-filtered ancestor.**
  Its canvas is `position: fixed`; any ancestor with `transform`, `filter`,
  or `backdrop-filter` re-anchors it away from the viewport. It also mounts
  **once** — the options passed to it are captured as a snapshot on first
  render (via a ref) and handed to `mount()` in a `useEffect` with an empty
  dependency array. Later prop changes are not reactive; there's no
  remount-on-change.
- **Tooltips (`b-tooltip[data-tip]`) position relative to their trigger** and
  will be clipped by any `overflow: hidden` ancestor — including `.b-card`
  and `.b-panel` without `.is-brackets` (brackets panels use
  `overflow: visible`).

## Commands (from `react/`)

- `npm test -- --run` — vitest, single pass (no watch)
- `npm run typecheck` — `tsc --noEmit`
- `npm run build` — esbuild bundle + declarations → `dist/`
- `npm run demo` — Vite demo harness; has an "app" view exercising every
  component plus a "LOGIN PREVIEW" toggle that swaps to the full-bleed
  `AmbientBackground` screen

## Consumers

1. `@vigilant/ui` → uploaded to claude.ai/design via `/design-sync`
2. The Vigilant Jinja2 site (sub-project 2) links `css/*.css` from `/static/`
