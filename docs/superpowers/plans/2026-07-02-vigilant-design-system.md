# Vigilant Design System Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the Vigilant Design System: shared CSS core (tokens/components/motion) in the expressive-brutalist style, the ambient New Eden flythrough background module, and the `@vigilant/ui` React library ready for `/design-sync` upload.

**Architecture:** One CSS source of truth in `design-system/css/` (the `b-*` vocabulary restyled with glass surfaces, gold corner brackets, and animated menus). A dependency-free canvas module renders the map flythrough with live ESI sov colors. A thin typed React library wraps the CSS classes and bundles everything (CSS + fonts + ambient) into a self-contained package built with esbuild.

**Tech Stack:** Plain CSS (custom properties), Canvas 2D (no WebGL), TypeScript + React 19, esbuild (lib build), tsc (declarations), vitest + @testing-library/react (tests), Vite (demo harness).

**Spec:** `docs/superpowers/specs/2026-07-02-vigilant-design-system-design.md`

**Reference look:** brainstorm mockups in `.superpowers/brainstorm/73208-1783035198/content/` — `motion-flair.html` (expressive tier), `mapfly-sov.html` (approved background). The current class vocabulary being restyled lives in `app/templates/base.html` lines 13–1194.

---

## File structure

```
design-system/
├── css/
│   ├── tokens.css            # Task 1 — custom properties
│   ├── motion.css            # Task 1 — keyframes + reduced-motion guards
│   └── components.css        # Tasks 2–3 — b-* vocabulary + new components
├── ambient/
│   ├── vigilant-ambient.js   # Task 4 — flythrough module (ESM)
│   └── vigilant-ambient.d.ts # Task 4 — hand-written declarations
└── react/
    ├── package.json          # Task 5
    ├── tsconfig.json         # Task 5
    ├── build.mjs             # Task 5 — esbuild lib build
    ├── vitest.config.ts      # Task 5
    ├── fonts/                # Task 9 — JetBrains Mono woff2 (from @fontsource)
    ├── src/
    │   ├── index.ts          # grows across Tasks 5–9
    │   ├── styles.css        # @imports of ../../css/* + fonts.css
    │   ├── fonts.css         # Task 9
    │   └── components/       # Tasks 5–9, one file per component
    ├── demo/                 # Task 10 — Vite harness
    └── dist/                 # build output (gitignored)
```

Line length note: all CSS below uses the compact one-line-per-declaration-group idiom already used in `base.html`.

---

### Task 1: Foundation stylesheets (tokens + motion)

**Goal:** Create `design-system/css/tokens.css` and `design-system/css/motion.css` — every custom property and keyframe the system uses.

**Files:**
- Create: `design-system/css/tokens.css`
- Create: `design-system/css/motion.css`

**Acceptance Criteria:**
- [ ] Both files parse cleanly under esbuild
- [ ] All tokens from the spec exist: base palette (kept from base.html), glass, elevation, motion, semantic, z-layers
- [ ] Every animation is disabled under `prefers-reduced-motion: reduce`

**Verify:** `cd design-system && npx esbuild css/tokens.css css/motion.css --bundle --outdir=/tmp/css-check --allow-overwrite` → exits 0, no warnings

**Steps:**

- [ ] **Step 1: Write `design-system/css/tokens.css`**

```css
/* Vigilant Design System — tokens
   Source of truth for both the Jinja2 site and @vigilant/ui.
   Base palette is carried over from app/templates/base.html verbatim;
   glass/elevation/motion tokens are new. */
:root {
    /* base palette (kept) */
    --bg:      #080808;
    --surface: #0e0e0e;
    --text:    #dedede;
    --muted:   #8a8a8a;
    --border:  #191919;
    --rule:    #dedede;
    --accent:  #c8a951;
    --danger:  #cc3333;
    --success: #33aa55;
    --warn:    #c8a951;
    --info:    #5a8fc4;

    /* accent variants */
    --accent-bright: #e8d9a8;
    --accent-dim:    rgba(200, 169, 81, 0.35);
    --accent-faint:  rgba(200, 169, 81, 0.08);

    /* glass surfaces */
    --glass-bg:       rgba(13, 13, 17, 0.55);
    --glass-bg-heavy: rgba(13, 13, 17, 0.80);
    --glass-border:   var(--accent-dim);
    --glass-blur:     8px;

    /* elevation (surface steps + shadows) */
    --surface-1: #0e0e0e;
    --surface-2: #101014;
    --surface-3: #14141a;
    --shadow-1: 0 4px 18px rgba(0, 0, 0, 0.5);
    --shadow-2: 0 8px 32px rgba(0, 0, 0, 0.55);
    --glow-accent:       0 0 16px rgba(200, 169, 81, 0.45);
    --glow-accent-soft:  0 0 24px rgba(200, 169, 81, 0.12);

    /* typography */
    --font-mono: 'JetBrains Mono', monospace;
    --fs-xs: 9px;  --fs-sm: 10px;  --fs-base: 12px;  --fs-md: 14px;
    --fs-lg: 18px; --fs-xl: 20px;
    --ls-tight: 0.04em; --ls-wide: 0.14em; --ls-wider: 0.18em; --ls-widest: 0.22em;

    /* spacing */
    --sp-1: 0.25rem; --sp-2: 0.5rem; --sp-3: 0.75rem; --sp-4: 1rem;
    --sp-5: 1.5rem;  --sp-6: 2rem;   --sp-7: 2.5rem;

    /* motion */
    --dur-fast: 180ms;
    --dur-menu: 280ms;
    --dur-slow: 500ms;
    --ease-std: cubic-bezier(0.2, 0.8, 0.2, 1);
    --ease-pop: cubic-bezier(0.2, 0.9, 0.25, 1.15);

    /* z-layers */
    --z-ambient: -1;
    --z-nav: 50;
    --z-dropdown: 60;
    --z-modal: 100;
    --z-toast: 110;
}
```

- [ ] **Step 2: Write `design-system/css/motion.css`**

```css
/* Vigilant Design System — motion
   All keyframes live here. Every consumer animation must reference these
   names; nothing else defines @keyframes. */

/* dropdown / overlay entrances */
@keyframes vg-menu-in {
    from { opacity: 0; transform: translateY(-8px) scaleY(0.92); }
    to   { opacity: 1; transform: translateY(0) scaleY(1); }
}
@keyframes vg-fade-up {
    from { opacity: 0; transform: translateY(6px); }
    to   { opacity: 1; transform: none; }
}
@keyframes vg-fade-in {
    from { opacity: 0; }
    to   { opacity: 1; }
}

/* accent effects */
@keyframes vg-sweep {
    0%   { opacity: 0.2; }
    50%  { opacity: 1; }
    100% { opacity: 0.2; }
}
@keyframes vg-shine {
    0%   { left: -60%; }
    55%  { left: 120%; }
    100% { left: 120%; }
}
@keyframes vg-glow-pulse {
    0%, 100% { box-shadow: var(--glow-accent-soft); }
    50%      { box-shadow: var(--glow-accent); }
}

/* terminal-style row entrance */
@keyframes vg-type-in {
    from { max-width: 0; opacity: 0.4; }
    to   { max-width: 100%; opacity: 1; }
}

/* skeleton shimmer */
@keyframes vg-shimmer {
    from { background-position: -200px 0; }
    to   { background-position: 200px 0; }
}

/* legacy names kept for site compat (base.html uses these) */
@keyframes spin { to { transform: rotate(360deg); } }
.spin-anim  { animation: spin 1.5s linear infinite; display: inline-block; }
@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }
.pulse-anim { animation: pulse 1.8s ease-in-out infinite; }

/* stagger helper: apply .vg-stagger to a container; direct children fade up */
.vg-stagger > * { animation: vg-fade-up var(--dur-slow) var(--ease-std) both; }
.vg-stagger > *:nth-child(2) { animation-delay: 60ms; }
.vg-stagger > *:nth-child(3) { animation-delay: 120ms; }
.vg-stagger > *:nth-child(4) { animation-delay: 180ms; }
.vg-stagger > *:nth-child(5) { animation-delay: 240ms; }
.vg-stagger > *:nth-child(n+6) { animation-delay: 300ms; }

/* kill switch — must remain the last rule block in this file */
@media (prefers-reduced-motion: reduce) {
    *, *::before, *::after {
        animation-duration: 0.01ms !important;
        animation-iteration-count: 1 !important;
        transition-duration: 0.01ms !important;
    }
}
```

- [ ] **Step 3: Verify**

Run: `cd design-system && npx esbuild css/tokens.css css/motion.css --bundle --outdir=/tmp/css-check --allow-overwrite`
Expected: exit 0, two files emitted, no warnings.

- [ ] **Step 4: Commit**

```bash
git add design-system/css/tokens.css design-system/css/motion.css
git commit -m "feat(design-system): foundation tokens and motion stylesheets"
```

---

### Task 2: Component CSS — restyled core vocabulary

**Goal:** Create `design-system/css/components.css` restyling the existing `b-*` classes to the expressive look (glass, brackets, animated menus) while keeping every class name.

**Files:**
- Create: `design-system/css/components.css`
- Reference (read-only): `app/templates/base.html:13-1194`

**Acceptance Criteria:**
- [ ] Every class in this list is defined: `b-nav b-nav-logo b-nav-links b-nav-link b-nav-dropdown b-nav-dropdown-menu b-nav-dropdown-item b-breadcrumbs b-crumb-sep b-crumb-current b-tab-strip b-footer b-footer-links b-footer-link b-footer-brand b-main b-page-header b-page-title b-section b-section-head b-label b-link b-eyebrow b-stats b-stat b-stat-val b-stat-label b-card b-card-top b-card-portrait b-card-info b-card-name b-card-sub b-card-body b-row b-row-label b-row-val b-actions b-btn b-table-row b-portrait-sm b-progress b-progress-fill b-dot b-badge b-banner b-server-bar b-grid-2 b-grid-3 b-panel b-panel-head b-empty b-muted b-muted-sm b-text b-pad-md`
- [ ] Dropdown menus animate in with `vg-menu-in`, have glass blur + gold top rule + light sweep
- [ ] `.b-panel.is-glass` and `.b-panel.is-brackets` modifiers exist (glass surface, gold corner brackets)
- [ ] `.b-btn.is-primary` (gold solid + shine sweep + hover glow/lift), `.is-ghost`, `.is-danger` variants exist
- [ ] Brutalist no-radius rule preserved: `:where([class^="b-"], [class*=" b-"]) { border-radius: 0; }` (except `.b-dot`)
- [ ] File `@import`s nothing — tokens are consumed, not imported (import order is the consumer's job)

**Verify:** `cd design-system && npx esbuild css/components.css --bundle --outdir=/tmp/css-check --allow-overwrite` → exit 0; then `for c in b-nav b-panel b-btn b-badge b-stats b-tab-strip b-banner; do grep -q "\.$c" css/components.css || echo "MISSING $c"; done` → no output

**Steps:**

- [ ] **Step 1: Write the file header, reset, and layout/nav sections**

Structural metrics (paddings, sizes, flex layouts) are carried over from `base.html`; only surfaces, borders, and motion change. Write:

```css
/* Vigilant Design System — components
   The b-* vocabulary. Class names match app/templates/base.html so the
   Jinja2 site adopts this file with zero template changes (sub-project 2).
   Requires tokens.css and motion.css loaded first. */

*, *::before, *::after { box-sizing: border-box; }
:where([class^="b-"], [class*=" b-"]) { border-radius: 0; }

body {
    background: var(--bg);
    color: var(--text);
    font-family: var(--font-mono);
    font-size: var(--fs-md);
    line-height: 1.65;
    min-height: 100vh;
    margin: 0;
}

/* ── Navigation ──────────────────────────────────────────────────── */
.b-nav {
    position: sticky; top: 0; z-index: var(--z-nav);
    background: rgba(8, 8, 10, 0.6);
    backdrop-filter: blur(10px);
    -webkit-backdrop-filter: blur(10px);
    border-bottom: 1px solid var(--glass-border);
    display: flex; align-items: center; justify-content: space-between;
    height: 46px; padding: 0 2rem;
}
.b-nav-logo {
    font-size: 13px; font-weight: 600; letter-spacing: var(--ls-widest);
    text-transform: uppercase; color: var(--text); text-decoration: none;
    transition: text-shadow var(--dur-fast);
}
.b-nav-logo:hover { text-shadow: 0 0 8px rgba(200, 169, 81, 0.6); }
.b-nav-links { display: flex; align-items: center; gap: 1.75rem; }
.b-nav-link {
    font-size: 11px; letter-spacing: var(--ls-wider); text-transform: uppercase;
    color: var(--muted); text-decoration: none; position: relative; padding-bottom: 3px;
    transition: color var(--dur-fast), text-shadow var(--dur-fast);
}
.b-nav-link::after {
    content: ''; position: absolute; left: 0; bottom: 0; width: 0; height: 1px;
    background: var(--accent); transition: width var(--dur-menu) var(--ease-std);
}
.b-nav-link:hover { color: var(--text); }
.b-nav-link:hover::after { width: 100%; }
.b-nav-link.is-active { color: var(--accent); font-weight: 500; }
.b-nav-link.is-danger:hover { color: var(--danger); }
.b-nav-link.is-add { color: var(--text); }

/* ── Nav dropdown (expressive) ───────────────────────────────────── */
.b-nav-dropdown { position: relative; }
.b-nav-dropdown-menu {
    display: none; position: absolute; top: 100%; left: 0; margin-top: 6px;
    padding: 0.4rem 0; min-width: 170px; z-index: var(--z-dropdown);
    background: var(--glass-bg-heavy);
    backdrop-filter: blur(var(--glass-blur));
    -webkit-backdrop-filter: blur(var(--glass-blur));
    border: 1px solid var(--glass-border);
    box-shadow: var(--shadow-2), var(--glow-accent-soft);
    transform-origin: top;
}
.b-nav-dropdown:hover .b-nav-dropdown-menu,
.b-nav-dropdown:focus-within .b-nav-dropdown-menu {
    display: block;
    animation: vg-menu-in var(--dur-menu) var(--ease-pop);
}
.b-nav-dropdown-menu::before {
    content: ''; position: absolute; top: 0; left: 0; right: 0; height: 1px;
    background: linear-gradient(90deg, transparent, var(--accent), transparent);
    animation: vg-sweep 2.2s linear infinite;
}
.b-nav-dropdown-item {
    display: block; padding: 0.4rem 0.75rem; font-size: 11px;
    letter-spacing: var(--ls-wide); text-transform: uppercase;
    color: var(--muted); text-decoration: none; white-space: nowrap;
    transition: color var(--dur-fast), background var(--dur-fast), letter-spacing var(--dur-fast);
}
.b-nav-dropdown-item:hover {
    color: var(--accent-bright);
    background: linear-gradient(90deg, rgba(200, 169, 81, 0.14), transparent);
    letter-spacing: 0.2em;
}
.b-nav-dropdown-item.is-active { color: var(--accent); font-weight: 500; }
```

- [ ] **Step 2: Append page furniture (breadcrumbs, tabs, footer, main, page header, section, labels)**

Carry the metrics from `base.html:126-259` and add the transitions:

```css
/* ── Breadcrumbs ─────────────────────────────────────────────────── */
.b-breadcrumbs { padding: 0.5rem 2rem; font-size: var(--fs-sm); letter-spacing: var(--ls-wide); text-transform: uppercase; color: var(--muted); }
.b-breadcrumbs a { color: var(--muted); text-decoration: none; transition: color var(--dur-fast); }
.b-breadcrumbs a:hover { color: var(--accent-bright); }
.b-breadcrumbs .b-crumb-sep { margin: 0 0.4rem; color: var(--border); }
.b-breadcrumbs .b-crumb-current { color: var(--text); }

/* ── Tab strip ───────────────────────────────────────────────────── */
.b-tab-strip { display: flex; gap: 0; border-bottom: 1px solid var(--border); margin-bottom: 1.25rem; overflow-x: auto; }
.b-tab-strip a, .b-tab-strip button {
    padding: 0.5rem 0.85rem; font-size: var(--fs-sm); letter-spacing: var(--ls-wide);
    text-transform: uppercase; color: var(--muted); text-decoration: none; white-space: nowrap;
    border: none; background: none; cursor: pointer; font-family: inherit;
    border-bottom: 2px solid transparent;
    transition: color var(--dur-fast), border-color var(--dur-fast), text-shadow var(--dur-fast);
}
.b-tab-strip a:hover, .b-tab-strip button:hover { color: var(--text); }
.b-tab-strip a.is-active, .b-tab-strip button.is-active {
    color: var(--accent); border-bottom-color: var(--accent); font-weight: 500;
    text-shadow: 0 0 10px rgba(200, 169, 81, 0.4);
}

/* ── Footer ──────────────────────────────────────────────────────── */
.b-footer { border-top: 1px solid var(--border); padding: 1.25rem 2rem; margin-top: 3rem; display: flex; align-items: center; justify-content: space-between; }
.b-footer-links { display: flex; gap: 1.5rem; }
.b-footer-link { font-size: var(--fs-sm); letter-spacing: var(--ls-wide); text-transform: uppercase; color: var(--muted); text-decoration: none; transition: color var(--dur-fast); }
.b-footer-link:hover { color: var(--text); }
.b-footer-brand { font-size: 11px; letter-spacing: var(--ls-wider); text-transform: uppercase; color: var(--muted); display: flex; align-items: center; gap: 0.4rem; }

/* ── Main layout / page header ───────────────────────────────────── */
.b-main { max-width: 1360px; margin: 0 auto; padding: 2.5rem 2rem; }
.b-page-header { display: flex; align-items: baseline; justify-content: space-between; padding-bottom: 1rem; margin-bottom: 2rem; border-bottom: 2px solid var(--rule); }
.b-page-title { font-size: var(--fs-xl); font-weight: 300; letter-spacing: 0.3em; text-transform: uppercase; color: var(--text); }

/* ── Section / labels ────────────────────────────────────────────── */
.b-section { margin-bottom: var(--sp-7); }
.b-section-head { display: flex; align-items: center; justify-content: space-between; padding: 0.55rem 0; border-top: 2px solid var(--rule); border-bottom: 1px solid var(--border); }
.b-label { font-size: 11px; font-weight: 600; letter-spacing: var(--ls-widest); text-transform: uppercase; color: var(--muted); }
.b-link { font-size: 11px; letter-spacing: var(--ls-wide); text-transform: uppercase; color: var(--muted); text-decoration: none; transition: color var(--dur-fast); }
.b-link:hover { color: var(--accent-bright); }
.b-eyebrow { font-size: var(--fs-sm); letter-spacing: var(--ls-widest); text-transform: uppercase; color: var(--accent); }

/* ── Text utilities (kept from base.html) ────────────────────────── */
.b-muted { color: var(--muted); font-size: var(--fs-base); }
.b-muted-sm { color: var(--muted); font-size: 11px; }
.b-text { font-size: var(--fs-base); color: var(--text); }
.b-pad-md { padding: var(--sp-3); }
.is-ok { color: var(--success) !important; }
.is-warn-text { color: var(--warn) !important; }
.is-danger-text { color: var(--danger) !important; }
```

- [ ] **Step 3: Append data display (stats, cards, rows, tables, progress, dot, badge)**

```css
/* ── Stats strip ─────────────────────────────────────────────────── */
.b-stats { display: flex; border: 1px solid var(--glass-border); background: var(--glass-bg); backdrop-filter: blur(var(--glass-blur)); -webkit-backdrop-filter: blur(var(--glass-blur)); margin-bottom: var(--sp-7); }
.b-stat { flex: 1; padding: 0.85rem 1rem; border-right: 1px solid var(--border); text-align: center; transition: background var(--dur-fast); }
.b-stat:last-child { border-right: none; }
.b-stat:hover { background: var(--accent-faint); }
.b-stat-val { font-size: var(--fs-lg); font-weight: 300; color: var(--text); letter-spacing: -0.01em; }
.b-stat-val.is-accent { color: var(--accent); }
.b-stat-val.is-danger { color: var(--danger); }
.b-stat-val.is-ok { color: var(--success); }
.b-stat-label { font-size: var(--fs-sm); letter-spacing: 0.2em; text-transform: uppercase; color: var(--muted); margin-top: 3px; }

/* ── Cards ───────────────────────────────────────────────────────── */
.b-card { border: 1px solid var(--border); background: linear-gradient(180deg, var(--surface-2), var(--surface-1)); overflow: hidden; transition: border-color var(--dur-menu), box-shadow var(--dur-menu), transform var(--dur-fast); }
.b-card:hover { border-color: var(--glass-border); box-shadow: var(--shadow-1); transform: translateY(-2px); }
.b-card.is-active { border-color: var(--accent); box-shadow: var(--glow-accent-soft); }
.b-card-top { display: flex; border-bottom: 1px solid var(--border); }
.b-card-portrait { width: 76px; height: 76px; object-fit: cover; object-position: top center; display: block; flex-shrink: 0; border-right: 1px solid var(--border); filter: grayscale(15%) contrast(1.05); }
.b-card-info { flex: 1; padding: 0.6rem 0.75rem; min-width: 0; display: flex; flex-direction: column; justify-content: center; gap: 3px; }
.b-card-name { font-size: 13px; font-weight: 600; letter-spacing: 0.06em; text-transform: uppercase; color: var(--text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.b-card-sub { font-size: 11px; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.b-card-body { }

/* ── Data rows ───────────────────────────────────────────────────── */
.b-row { display: flex; align-items: center; justify-content: space-between; padding: 0.3rem 0.75rem; border-bottom: 1px solid var(--border); gap: 0.5rem; min-height: 26px; }
.b-row:last-child { border-bottom: none; }
.b-row-label { font-size: var(--fs-sm); letter-spacing: var(--ls-wider); text-transform: uppercase; color: var(--muted); flex-shrink: 0; }
.b-row-val { font-size: var(--fs-base); color: var(--text); text-align: right; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.b-row-val.is-accent { color: var(--accent); }
.b-row-val.is-muted { color: var(--muted); }
.b-row-val.is-danger { color: var(--danger); }
.b-row-val.is-warn { color: var(--warn); }
.b-row-val.is-ok { color: var(--success); }

/* ── Table rows ──────────────────────────────────────────────────── */
.b-table-row { display: flex; align-items: center; padding: 0.35rem 0.75rem; border-bottom: 1px solid var(--border); gap: 0.6rem; min-height: 30px; transition: background var(--dur-fast); }
.b-table-row:last-child { border-bottom: none; }
.b-table-row:hover { background: var(--accent-faint); }
.b-portrait-sm { width: 20px; height: 20px; object-fit: cover; border: 1px solid var(--border); flex-shrink: 0; filter: grayscale(15%); }

/* ── Progress / dot / badge ──────────────────────────────────────── */
.b-progress { height: 2px; background: var(--border); margin-top: 4px; overflow: hidden; }
.b-progress-fill { height: 100%; background: var(--muted); transition: width var(--dur-slow) var(--ease-std); }
.b-progress-fill.is-active { background: var(--text); }
.b-progress-fill.is-warn { background: var(--accent); box-shadow: 0 0 6px rgba(200, 169, 81, 0.6); }
.b-progress-fill.is-crit { background: var(--danger); box-shadow: 0 0 6px rgba(204, 51, 51, 0.6); }
.b-dot { width: 6px; height: 6px; border-radius: 50%; display: inline-block; flex-shrink: 0; }
.b-dot.is-ok { background: var(--success); box-shadow: 0 0 5px rgba(51, 170, 85, 0.8); }
.b-dot.is-warn { background: var(--accent); box-shadow: 0 0 5px rgba(200, 169, 81, 0.8); }
.b-dot.is-error { background: var(--danger); box-shadow: 0 0 5px rgba(204, 51, 51, 0.8); }
.b-dot.is-muted { background: var(--muted); }
.b-badge { font-size: var(--fs-sm); letter-spacing: var(--ls-wide); text-transform: uppercase; padding: 1px 5px; border: 1px solid currentColor; white-space: nowrap; }
.b-badge.is-active { background: var(--text); color: var(--bg); border-color: var(--text); }
.b-badge.is-warn { color: var(--accent); }
.b-badge.is-danger { color: var(--danger); }
.b-badge.is-ok { color: var(--success); }
```

- [ ] **Step 4: Append panels (with glass/bracket modifiers), buttons, banner, grids, misc**

```css
/* ── Panels ──────────────────────────────────────────────────────── */
.b-panel { border: 1px solid var(--border); background: linear-gradient(180deg, var(--surface-2), var(--surface-1)); overflow: hidden; transition: border-color var(--dur-menu), box-shadow var(--dur-menu); }
.b-panel:hover { border-color: rgba(200, 169, 81, 0.25); }
.b-panel.is-glass { background: var(--glass-bg); backdrop-filter: blur(var(--glass-blur)); -webkit-backdrop-filter: blur(var(--glass-blur)); border-color: var(--glass-border); box-shadow: var(--shadow-2), var(--glow-accent-soft); }
.b-panel.is-brackets { position: relative; overflow: visible; }
.b-panel.is-brackets::before { content: ''; position: absolute; top: -1px; left: -1px; width: 14px; height: 14px; border-top: 2px solid var(--accent); border-left: 2px solid var(--accent); pointer-events: none; }
.b-panel.is-brackets::after { content: ''; position: absolute; bottom: -1px; right: -1px; width: 14px; height: 14px; border-bottom: 2px solid var(--accent); border-right: 2px solid var(--accent); pointer-events: none; }
.b-panel-head { display: flex; align-items: center; justify-content: space-between; padding: 0.55rem 0.75rem; border-bottom: 1px solid var(--glass-border); }
.b-panel-head .b-label { color: var(--accent); }
.b-empty { padding: 1.25rem 0.75rem; text-align: center; color: var(--muted); font-size: 11px; letter-spacing: var(--ls-wide); text-transform: uppercase; }

/* ── Buttons ─────────────────────────────────────────────────────── */
/* .b-btn default = card action-strip button (kept from base.html) */
.b-btn { flex: 1; padding: 0.38rem 0.5rem; text-align: center; font-size: var(--fs-sm); letter-spacing: 0.16em; text-transform: uppercase; color: var(--muted); border: none; border-right: 1px solid var(--border); background: none; cursor: pointer; font-family: inherit; text-decoration: none; display: block; transition: background var(--dur-fast), color var(--dur-fast); }
.b-btn:last-child { border-right: none; }
.b-btn:hover { background: var(--border); color: var(--text); }
.b-btn.is-danger:hover { color: var(--danger); }
.b-btn.is-warn { color: var(--accent); }
.b-btn.is-warn:hover { background: var(--border); color: var(--accent); }
/* standalone variants (new) */
.b-btn.is-primary, .b-btn.is-ghost { flex: none; display: inline-block; padding: 0.45rem 1.1rem; border: 1px solid transparent; }
.b-btn.is-primary { background: var(--accent); color: var(--bg); font-weight: 600; position: relative; overflow: hidden; transition: box-shadow var(--dur-menu), transform var(--dur-fast); }
.b-btn.is-primary::after { content: ''; position: absolute; top: 0; left: -60%; width: 40%; height: 100%; background: linear-gradient(100deg, transparent, rgba(255, 255, 255, 0.5), transparent); animation: vg-shine 2.6s ease infinite; }
.b-btn.is-primary:hover { background: var(--accent); color: var(--bg); box-shadow: var(--glow-accent); transform: translateY(-1px); }
.b-btn.is-ghost { border-color: var(--glass-border); color: var(--text); background: var(--glass-bg); backdrop-filter: blur(var(--glass-blur)); -webkit-backdrop-filter: blur(var(--glass-blur)); transition: border-color var(--dur-fast), box-shadow var(--dur-menu), color var(--dur-fast); }
.b-btn.is-ghost:hover { border-color: var(--accent); color: var(--accent-bright); box-shadow: var(--glow-accent-soft); background: var(--glass-bg); }
.b-btn.is-ghost.is-danger { color: var(--danger); }
.b-btn.is-ghost.is-danger:hover { border-color: var(--danger); box-shadow: 0 0 16px rgba(204, 51, 51, 0.35); }
.b-actions { display: flex; border-top: 1px solid var(--border); }

/* ── Banner / server bar / grids ─────────────────────────────────── */
.b-banner { border: 1px solid var(--accent); border-left: 3px solid var(--accent); background: var(--accent-faint); padding: 0.5rem 0.75rem; margin-bottom: 1.5rem; display: flex; align-items: center; justify-content: space-between; gap: 1rem; font-size: var(--fs-base); animation: vg-fade-up var(--dur-slow) var(--ease-std); }
.b-banner.is-danger { border-color: var(--danger); background: rgba(204, 51, 51, 0.08); }
.b-banner.is-ok { border-color: var(--success); background: rgba(51, 170, 85, 0.08); }
.b-server-bar { display: flex; align-items: center; justify-content: space-between; padding: 0.45rem 0; border-bottom: 1px solid var(--border); margin-bottom: 2rem; font-size: 11px; letter-spacing: var(--ls-wide); text-transform: uppercase; }
.b-grid-3 { display: grid; grid-template-columns: repeat(3, 1fr); gap: 1px; background: var(--border); border: 1px solid var(--border); }
.b-grid-3 > * { background: var(--surface); }
.b-grid-2 { display: grid; grid-template-columns: repeat(2, 1fr); gap: 1.5rem; }

/* ── Scrollbar ───────────────────────────────────────────────────── */
::-webkit-scrollbar { width: 3px; height: 3px; }
::-webkit-scrollbar-track { background: var(--bg); }
::-webkit-scrollbar-thumb { background: var(--accent-dim); }
```

- [ ] **Step 5: Verify**

Run: `cd design-system && npx esbuild css/components.css --bundle --outdir=/tmp/css-check --allow-overwrite && for c in b-nav b-panel b-btn b-badge b-stats b-tab-strip b-banner b-grid-2 b-eyebrow; do grep -q "\.$c" css/components.css || echo "MISSING $c"; done`
Expected: exit 0, no "MISSING" lines.

- [ ] **Step 6: Commit**

```bash
git add design-system/css/components.css
git commit -m "feat(design-system): restyled b-* component vocabulary (glass, brackets, animated menus)"
```

---

### Task 3: Component CSS — new components

**Goal:** Append the components the site lacks today: modal, toast, tooltip, skeleton, switch, checkbox, inputs, select.

**Files:**
- Modify: `design-system/css/components.css` (append)

**Acceptance Criteria:**
- [ ] Classes exist: `b-modal-overlay b-modal b-toast-stack b-toast b-tooltip b-skeleton b-switch b-check b-input b-select b-field b-field-label`
- [ ] Modal: fixed overlay with fade-in, glass panel with brackets, `vg-menu-in` entrance
- [ ] Toast: glass, tone left-border, slides in with `vg-fade-up`
- [ ] Inputs: dark field, gold focus border + soft glow, no radius

**Verify:** `cd design-system && npx esbuild css/components.css --bundle --outdir=/tmp/css-check --allow-overwrite && for c in b-modal b-toast b-tooltip b-skeleton b-switch b-input b-select; do grep -q "\.$c" css/components.css || echo "MISSING $c"; done` → exit 0, no output

**Steps:**

- [ ] **Step 1: Append to `design-system/css/components.css`**

```css
/* ── Modal ───────────────────────────────────────────────────────── */
.b-modal-overlay { position: fixed; inset: 0; z-index: var(--z-modal); background: rgba(4, 4, 8, 0.7); backdrop-filter: blur(3px); -webkit-backdrop-filter: blur(3px); display: flex; align-items: center; justify-content: center; padding: var(--sp-6); animation: vg-fade-in var(--dur-fast) ease; }
.b-modal { width: min(560px, 100%); max-height: 85vh; overflow-y: auto; background: var(--glass-bg-heavy); backdrop-filter: blur(var(--glass-blur)); -webkit-backdrop-filter: blur(var(--glass-blur)); border: 1px solid var(--glass-border); box-shadow: var(--shadow-2), var(--glow-accent-soft); position: relative; animation: vg-menu-in var(--dur-menu) var(--ease-pop); transform-origin: top; }
.b-modal::before { content: ''; position: absolute; top: -1px; left: -1px; width: 14px; height: 14px; border-top: 2px solid var(--accent); border-left: 2px solid var(--accent); }
.b-modal::after { content: ''; position: absolute; bottom: -1px; right: -1px; width: 14px; height: 14px; border-bottom: 2px solid var(--accent); border-right: 2px solid var(--accent); }
.b-modal-head { display: flex; align-items: center; justify-content: space-between; padding: 0.65rem 1rem; border-bottom: 1px solid var(--glass-border); }
.b-modal-head .b-label { color: var(--accent); }
.b-modal-body { padding: 1rem; font-size: var(--fs-base); }
.b-modal-close { background: none; border: none; color: var(--muted); font-size: 16px; cursor: pointer; padding: 0 0.3rem; line-height: 1; font-family: inherit; transition: color var(--dur-fast), text-shadow var(--dur-fast); }
.b-modal-close:hover { color: var(--text); text-shadow: 0 0 8px rgba(222, 222, 222, 0.5); }

/* ── Toast ───────────────────────────────────────────────────────── */
.b-toast-stack { position: fixed; right: 20px; bottom: 20px; z-index: var(--z-toast); display: flex; flex-direction: column; gap: 6px; width: 300px; }
.b-toast { background: var(--glass-bg-heavy); backdrop-filter: blur(var(--glass-blur)); -webkit-backdrop-filter: blur(var(--glass-blur)); border: 1px solid var(--glass-border); border-left: 3px solid var(--accent); padding: 0.5rem 0.75rem; font-size: 11px; letter-spacing: var(--ls-tight); color: var(--text); box-shadow: var(--shadow-1); animation: vg-fade-up var(--dur-menu) var(--ease-std); }
.b-toast.is-danger { border-left-color: var(--danger); }
.b-toast.is-ok { border-left-color: var(--success); }
.b-toast.is-info { border-left-color: var(--info); }

/* ── Tooltip (CSS-only, data-tip attr) ───────────────────────────── */
.b-tooltip { position: relative; }
.b-tooltip::after { content: attr(data-tip); position: absolute; bottom: calc(100% + 6px); left: 50%; transform: translateX(-50%) translateY(4px); background: var(--glass-bg-heavy); backdrop-filter: blur(var(--glass-blur)); border: 1px solid var(--glass-border); color: var(--text); font-size: var(--fs-sm); letter-spacing: var(--ls-tight); padding: 3px 8px; white-space: nowrap; opacity: 0; pointer-events: none; transition: opacity var(--dur-fast), transform var(--dur-fast); z-index: var(--z-dropdown); }
.b-tooltip:hover::after { opacity: 1; transform: translateX(-50%) translateY(0); }

/* ── Skeleton ────────────────────────────────────────────────────── */
.b-skeleton { height: 12px; background: linear-gradient(90deg, var(--surface-2) 25%, var(--surface-3) 50%, var(--surface-2) 75%); background-size: 400px 100%; animation: vg-shimmer 1.4s linear infinite; }
.b-skeleton + .b-skeleton { margin-top: 8px; }

/* ── Switch / checkbox ───────────────────────────────────────────── */
.b-switch { appearance: none; width: 30px; height: 16px; border: 1px solid var(--border); background: var(--surface-1); position: relative; cursor: pointer; transition: border-color var(--dur-fast); vertical-align: middle; }
.b-switch::before { content: ''; position: absolute; top: 2px; left: 2px; width: 10px; height: 10px; background: var(--muted); transition: left var(--dur-fast) var(--ease-std), background var(--dur-fast); }
.b-switch:checked { border-color: var(--accent); }
.b-switch:checked::before { left: 16px; background: var(--accent); box-shadow: 0 0 6px rgba(200, 169, 81, 0.7); }
.b-check { appearance: none; width: 14px; height: 14px; border: 1px solid var(--border); background: var(--surface-1); cursor: pointer; position: relative; transition: border-color var(--dur-fast); vertical-align: middle; }
.b-check:checked { border-color: var(--accent); }
.b-check:checked::before { content: ''; position: absolute; inset: 3px; background: var(--accent); }

/* ── Inputs ──────────────────────────────────────────────────────── */
.b-field { display: flex; flex-direction: column; gap: 4px; }
.b-field-label { font-size: var(--fs-sm); letter-spacing: var(--ls-wider); text-transform: uppercase; color: var(--muted); }
.b-input, .b-select { font-family: inherit; font-size: var(--fs-base); color: var(--text); background: var(--surface-1); border: 1px solid var(--border); padding: 0.45rem 0.6rem; outline: none; transition: border-color var(--dur-fast), box-shadow var(--dur-menu); }
.b-input::placeholder { color: var(--muted); }
.b-input:focus, .b-select:focus { border-color: var(--accent); box-shadow: var(--glow-accent-soft); }
.b-select { appearance: none; background-image: linear-gradient(45deg, transparent 50%, var(--muted) 50%), linear-gradient(135deg, var(--muted) 50%, transparent 50%); background-position: calc(100% - 14px) 50%, calc(100% - 10px) 50%; background-size: 4px 4px; background-repeat: no-repeat; padding-right: 26px; }
```

- [ ] **Step 2: Verify**

Run: `cd design-system && npx esbuild css/components.css --bundle --outdir=/tmp/css-check --allow-overwrite && for c in b-modal b-toast b-tooltip b-skeleton b-switch b-input b-select b-field; do grep -q "\.$c" css/components.css || echo "MISSING $c"; done`
Expected: exit 0, no output.

- [ ] **Step 3: Commit**

```bash
git add design-system/css/components.css
git commit -m "feat(design-system): modal, toast, tooltip, skeleton, switch, and input components"
```

---

### Task 4: Ambient background module

**Goal:** Productionize the approved `mapfly-sov.html` flythrough as a dependency-free ES module with mount/destroy API, live sov colors, pluggable kill source, and performance guards.

**Files:**
- Create: `design-system/ambient/vigilant-ambient.js`
- Create: `design-system/ambient/vigilant-ambient.d.ts`

**Acceptance Criteria:**
- [ ] `node --check` passes (valid syntax) and `node --input-type=module -e "import('./design-system/ambient/vigilant-ambient.js').then(m => { if (typeof m.mount !== 'function') throw new Error('no mount'); })"` passes
- [ ] Exports `mount(el, options)` returning `{ destroy() }`
- [ ] Options: `systemsUrl` (default `/static/data/systems.json`), `killSource` (`{type:'simulate'}` default, or `{type:'poll', url, intervalMs}`), `minWidth` (default 768), `fpsCap` (default 30)
- [ ] ESI sov fetched with 24h localStorage cache; all fetch failures degrade to neutral colors silently
- [ ] Pauses rendering when `document.hidden`; does nothing at all under `prefers-reduced-motion` or viewport < `minWidth`
- [ ] No DOM assumptions beyond the passed element; canvas created and removed by the module

**Verify:** `node --check design-system/ambient/vigilant-ambient.js && node --input-type=module -e "import(process.cwd()+'/design-system/ambient/vigilant-ambient.js').then(m=>{if(typeof m.mount!=='function')throw new Error('no mount');console.log('OK')})"` → prints `OK`

**Steps:**

- [ ] **Step 1: Write `design-system/ambient/vigilant-ambient.js`**

The renderer logic (normalization, camera basis, projection, fog, blips, sov coloring) is a direct port of the approved demo generator (`.superpowers/brainstorm/73208-1783035198/content/mapfly-sov.html`); the module adds lifecycle, guards, and data loading:

```js
/* Vigilant Ambient — flying through New Eden with live sov colors and kill blips.
   Dependency-free ES module. Canvas 2D. See design spec 2026-07-02. */

const ESI_SOV_URL = 'https://esi.evetech.net/latest/sovereignty/map/?datasource=tranquility';
const SOV_CACHE_KEY = 'vg-ambient-sov-v1';
const SOV_TTL_MS = 24 * 60 * 60 * 1000;

const FACTION_COLORS = {
  500001: [74, 144, 217],  // Caldari State
  500002: [179, 74, 58],   // Minmatar Republic
  500003: [230, 190, 90],  // Amarr Empire
  500004: [88, 191, 117],  // Gallente Federation
  500026: [200, 60, 60],   // Triglavian
};
const NEUTRAL = [190, 195, 205];

function hslToRgb(h, s, l) {
  const c = (1 - Math.abs(2 * l - 1)) * s, x = c * (1 - Math.abs(((h / 60) % 2) - 1)), m = l - c / 2;
  let r, g, b;
  if (h < 60) [r, g, b] = [c, x, 0]; else if (h < 120) [r, g, b] = [x, c, 0];
  else if (h < 180) [r, g, b] = [0, c, x]; else if (h < 240) [r, g, b] = [0, x, c];
  else if (h < 300) [r, g, b] = [x, 0, c]; else [r, g, b] = [c, 0, x];
  return [Math.round((r + m) * 255), Math.round((g + m) * 255), Math.round((b + m) * 255)];
}
export function allianceColor(id) { return hslToRgb((id * 137.508) % 360, 0.62, 0.58); }

export function normalizeSystems(raw) {
  // raw: array of {id, name, sec, x3, y3, z3} (frontend/public/data/systems.json shape)
  const n = raw.length;
  let cx = 0, cy = 0, cz = 0;
  for (const s of raw) { cx += s.x3; cy += s.y3; cz += s.z3; }
  cx /= n; cy /= n; cz /= n;
  let ext = 0;
  for (const s of raw) ext = Math.max(ext, Math.abs(s.x3 - cx), Math.abs(s.z3 - cz));
  const k = 1000 / ext;
  return raw.map((s) => ({
    id: s.id, name: s.name,
    x: (s.x3 - cx) * k, y: (s.y3 - cy) * k, z: (s.z3 - cz) * k,
  }));
}

async function loadSovColors(systems) {
  const byId = new Map(systems.map((s, i) => [s.id, i]));
  const cols = systems.map(() => NEUTRAL);
  let sov = null;
  try {
    const cached = JSON.parse(localStorage.getItem(SOV_CACHE_KEY) || 'null');
    if (cached && Date.now() - cached.t < SOV_TTL_MS) sov = cached.d;
  } catch (e) { /* localStorage unavailable — fall through to fetch */ }
  if (!sov) {
    try {
      const r = await fetch(ESI_SOV_URL);
      if (!r.ok) return cols;
      sov = await r.json();
      try { localStorage.setItem(SOV_CACHE_KEY, JSON.stringify({ t: Date.now(), d: sov })); } catch (e) { /* quota */ }
    } catch (e) { return cols; }
  }
  for (const e of sov) {
    const i = byId.get(e.system_id);
    if (i === undefined) continue;
    if (e.alliance_id) cols[i] = allianceColor(e.alliance_id);
    else if (e.faction_id && FACTION_COLORS[e.faction_id]) cols[i] = FACTION_COLORS[e.faction_id];
  }
  return cols;
}

export function mount(el, options = {}) {
  const opts = {
    systemsUrl: '/static/data/systems.json',
    killSource: { type: 'simulate' },
    minWidth: 768,
    fpsCap: 30,
    speed: 0.00012,
    ...options,
  };

  const reduced = typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (reduced || window.innerWidth < opts.minWidth) return { destroy() {} };

  const canvas = document.createElement('canvas');
  canvas.style.cssText = 'position:fixed;inset:0;width:100%;height:100%;z-index:var(--z-ambient,-1);pointer-events:none;';
  el.appendChild(canvas);
  const ctx = canvas.getContext('2d');

  let W = 0, H = 0, raf = 0, killTimer = 0, destroyed = false;
  let systems = [], cols = [], kill = null;
  let t = 0, last = 0;
  const frameMs = 1000 / opts.fpsCap;
  const RX = 520, RZ = 420, CAMY = 120, FOV = 700, NEAR = 20, FAR = 1600;

  function resize() { W = canvas.width = innerWidth; H = canvas.height = innerHeight; }
  resize();
  addEventListener('resize', resize);

  function camPos(tt) {
    const a = tt * Math.PI * 2;
    return [Math.cos(a) * RX, CAMY + Math.sin(a * 3) * 30, Math.sin(a) * RZ];
  }

  function frame(now) {
    if (destroyed) return;
    raf = requestAnimationFrame(frame);
    if (document.hidden || now - last < frameMs) return;
    last = now;
    t += opts.speed;
    const cam = camPos(t), ahead = camPos(t + 0.012);
    let fx = ahead[0] - cam[0], fy = ahead[1] - cam[1] - 40, fz = ahead[2] - cam[2];
    const fl = Math.hypot(fx, fy, fz); fx /= fl; fy /= fl; fz /= fl;
    let rx = fz, rz = -fx;
    const rl = Math.hypot(rx, rz) || 1; rx /= rl; rz /= rl;
    const ux = -rz * fy, uy = rz * fx - rx * fz, uz = rx * fy;

    ctx.fillStyle = '#04040a'; ctx.fillRect(0, 0, W, H);
    const g = ctx.createRadialGradient(W * 0.7, H * 0.35, 0, W * 0.7, H * 0.35, W * 0.6);
    g.addColorStop(0, 'rgba(90,70,140,.05)'); g.addColorStop(1, 'rgba(0,0,0,0)');
    ctx.fillStyle = g; ctx.fillRect(0, 0, W, H);

    for (let i = 0; i < systems.length; i++) {
      const p = systems[i];
      const dx = p.x - cam[0], dy = p.y - cam[1], dz = p.z - cam[2];
      const z = dx * fx + dy * fy + dz * fz;
      if (z < NEAR || z > FAR) { if (kill) kill[i] *= 0.98; continue; }
      const x = dx * rx + dz * rz;
      const y = dx * ux + dy * uy + dz * uz;
      const k = FOV / z;
      const sx = W / 2 + x * k, sy = H / 2 - y * k;
      if (sx < -30 || sx > W + 30 || sy < -30 || sy > H + 30) { if (kill) kill[i] *= 0.98; continue; }
      let fog = 1 - z / FAR; fog *= fog;
      const rad = Math.max(0.5, 2.6 * k * 0.55);
      const c = cols[i] || NEUTRAL;
      ctx.fillStyle = `rgba(${c[0]},${c[1]},${c[2]},${(0.25 + 0.65 * fog).toFixed(3)})`;
      ctx.beginPath(); ctx.arc(sx, sy, rad, 0, 7); ctx.fill();
      if (k > 0.5) {
        ctx.fillStyle = `rgba(${c[0]},${c[1]},${c[2]},${(0.08 * fog).toFixed(3)})`;
        ctx.beginPath(); ctx.arc(sx, sy, rad * 3.2, 0, 7); ctx.fill();
      }
      if (kill && kill[i] > 0.01) {
        const kr = (1 - kill[i]) * 46 * Math.min(k, 2) + rad + 2;
        ctx.strokeStyle = `rgba(255,70,70,${(kill[i] * 0.95).toFixed(3)})`; ctx.lineWidth = 1.6;
        ctx.beginPath(); ctx.arc(sx, sy, kr, 0, 7); ctx.stroke();
        ctx.fillStyle = `rgba(255,90,90,${(kill[i] * 0.9).toFixed(3)})`;
        ctx.beginPath(); ctx.arc(sx, sy, rad + 2, 0, 7); ctx.fill();
        kill[i] *= 0.988;
      }
    }
  }

  function flare(systemId) {
    if (!kill) return;
    for (let i = 0; i < systems.length; i++) {
      if (systems[i].id === systemId) { kill[i] = 1; return; }
    }
  }

  function startKills() {
    const src = opts.killSource;
    if (src.type === 'simulate') {
      const tick = () => {
        if (destroyed) return;
        // flare a random system roughly ahead of the camera
        const cam = camPos(t), ahead = camPos(t + 0.012);
        let fx = ahead[0] - cam[0], fz = ahead[2] - cam[2];
        const fl = Math.hypot(fx, fz); fx /= fl; fz /= fl;
        for (let tries = 0; tries < 40; tries++) {
          const i = (Math.random() * systems.length) | 0;
          const z = (systems[i].x - cam[0]) * fx + (systems[i].z - cam[2]) * fz;
          if (z > 100 && z < 900) { kill[i] = 1; break; }
        }
        killTimer = setTimeout(tick, 900 + Math.random() * 1800);
      };
      killTimer = setTimeout(tick, 700);
    } else if (src.type === 'poll') {
      const tick = async () => {
        if (destroyed) return;
        try {
          const r = await fetch(src.url);
          if (r.ok) (await r.json()).forEach((k) => flare(k.system_id ?? k));
        } catch (e) { /* silent */ }
        killTimer = setTimeout(tick, src.intervalMs || 15000);
      };
      killTimer = setTimeout(tick, 1000);
    }
  }

  (async () => {
    try {
      const r = await fetch(opts.systemsUrl);
      if (!r.ok) return;
      systems = normalizeSystems(await r.json());
      kill = new Float32Array(systems.length);
      cols = await loadSovColors(systems);
      if (destroyed) return;
      startKills();
      raf = requestAnimationFrame(frame);
    } catch (e) { /* no background — page still works */ }
  })();

  return {
    flare,
    destroy() {
      destroyed = true;
      cancelAnimationFrame(raf);
      clearTimeout(killTimer);
      removeEventListener('resize', resize);
      canvas.remove();
    },
  };
}
```

- [ ] **Step 2: Write `design-system/ambient/vigilant-ambient.d.ts`**

```ts
export interface AmbientKillSourceSimulate { type: 'simulate' }
export interface AmbientKillSourcePoll { type: 'poll'; url: string; intervalMs?: number }
export type AmbientKillSource = AmbientKillSourceSimulate | AmbientKillSourcePoll;

export interface AmbientOptions {
  systemsUrl?: string;
  killSource?: AmbientKillSource;
  minWidth?: number;
  fpsCap?: number;
  speed?: number;
}

export interface AmbientHandle {
  flare?(systemId: number): void;
  destroy(): void;
}

export function mount(el: HTMLElement, options?: AmbientOptions): AmbientHandle;
export function allianceColor(id: number): [number, number, number];
export function normalizeSystems(
  raw: Array<{ id: number; name: string; x3: number; y3: number; z3: number }>
): Array<{ id: number; name: string; x: number; y: number; z: number }>;
```

- [ ] **Step 3: Verify**

Run: `node --check design-system/ambient/vigilant-ambient.js && node --input-type=module -e "import(process.cwd()+'/design-system/ambient/vigilant-ambient.js').then(m=>{if(typeof m.mount!=='function')throw new Error('no mount');const c=m.allianceColor(99003581);if(!Array.isArray(c)||c.length!==3)throw new Error('bad color');const s=m.normalizeSystems([{id:1,name:'A',x3:0,y3:0,z3:0},{id:2,name:'B',x3:10,y3:1,z3:5}]);if(s.length!==2)throw new Error('bad normalize');console.log('OK')})"`
Expected: prints `OK`.

- [ ] **Step 4: Commit**

```bash
git add design-system/ambient/
git commit -m "feat(design-system): ambient New Eden flythrough module with live sov colors"
```

---

### Task 5: React package scaffold + build pipeline + Button

**Goal:** Create the `@vigilant/ui` package with working esbuild build, tsc declarations, vitest, and the first component (Button) proving the whole pipeline.

**Files:**
- Create: `design-system/react/package.json`
- Create: `design-system/react/tsconfig.json`
- Create: `design-system/react/build.mjs`
- Create: `design-system/react/vitest.config.ts`
- Create: `design-system/react/src/styles.css`
- Create: `design-system/react/src/index.ts`
- Create: `design-system/react/src/components/Button.tsx`
- Create: `design-system/react/src/components/__tests__/Button.test.tsx`
- Create: `design-system/react/.gitignore`

**Acceptance Criteria:**
- [ ] `npm test` green
- [ ] `npm run build` emits `dist/index.js` (ESM), `dist/index.css` (bundled tokens+motion+components), `dist/index.d.ts`
- [ ] `tsc --noEmit` clean
- [ ] Button renders `b-btn` with `is-primary` / `is-ghost` / `is-danger` variant classes

**Verify:** `cd design-system/react && npm install && npm test -- --run && npm run build && ls dist/index.js dist/index.css dist/index.d.ts` → all three files listed

**Steps:**

- [ ] **Step 1: Write `design-system/react/package.json`**

```json
{
  "name": "@vigilant/ui",
  "version": "0.1.0",
  "private": true,
  "type": "module",
  "description": "Vigilant design system — expressive brutalist EVE dashboard components",
  "main": "dist/index.js",
  "module": "dist/index.js",
  "types": "dist/index.d.ts",
  "files": ["dist", "fonts"],
  "scripts": {
    "build": "node build.mjs && tsc --emitDeclarationOnly --declaration --outDir dist",
    "typecheck": "tsc --noEmit",
    "test": "vitest",
    "demo": "vite demo --open"
  },
  "peerDependencies": {
    "react": ">=18",
    "react-dom": ">=18"
  },
  "devDependencies": {
    "@fontsource/jetbrains-mono": "^5.2.5",
    "@testing-library/react": "^16.3.0",
    "@types/react": "^19.2.14",
    "@types/react-dom": "^19.2.3",
    "esbuild": "^0.25.0",
    "jsdom": "^26.0.0",
    "react": "^19.2.4",
    "react-dom": "^19.2.4",
    "typescript": "~5.9.3",
    "vite": "^8.0.1",
    "vitest": "^3.0.0"
  }
}
```

- [ ] **Step 2: Write `design-system/react/tsconfig.json`**

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "ESNext",
    "moduleResolution": "bundler",
    "jsx": "react-jsx",
    "strict": true,
    "skipLibCheck": true,
    "esModuleInterop": true,
    "isolatedModules": true,
    "types": ["vitest/globals"],
    "allowJs": true
  },
  "include": ["src", "../ambient/vigilant-ambient.d.ts"]
}
```

- [ ] **Step 3: Write `design-system/react/build.mjs`**

```js
import * as esbuild from 'esbuild';

await esbuild.build({
  entryPoints: ['src/index.ts'],
  outfile: 'dist/index.js',
  bundle: true,
  format: 'esm',
  jsx: 'automatic',
  external: ['react', 'react-dom', 'react/jsx-runtime'],
  loader: { '.woff2': 'file' },
  logLevel: 'info',
});

await esbuild.build({
  entryPoints: ['src/styles.css'],
  outfile: 'dist/index.css',
  bundle: true,
  loader: { '.woff2': 'file' },
  logLevel: 'info',
});
```

- [ ] **Step 4: Write `design-system/react/vitest.config.ts`**

```ts
import { defineConfig } from 'vitest/config';

export default defineConfig({
  esbuild: { jsx: 'automatic' },
  test: {
    environment: 'jsdom',
    globals: true,
    include: ['src/**/*.test.tsx'],
  },
});
```

- [ ] **Step 5: Write `design-system/react/src/styles.css`** (fonts.css joins in Task 9)

```css
@import "../../css/tokens.css";
@import "../../css/motion.css";
@import "../../css/components.css";
```

- [ ] **Step 6: Write the failing test `design-system/react/src/components/__tests__/Button.test.tsx`**

```tsx
import { render, screen } from '@testing-library/react';
import { Button } from '../Button';

test('renders primary variant with shine classes', () => {
  render(<Button variant="primary">Scan</Button>);
  const btn = screen.getByRole('button', { name: 'Scan' });
  expect(btn.className).toContain('b-btn');
  expect(btn.className).toContain('is-primary');
});

test('renders ghost and danger variants', () => {
  render(<Button variant="ghost" danger>Delete</Button>);
  const btn = screen.getByRole('button', { name: 'Delete' });
  expect(btn.className).toContain('is-ghost');
  expect(btn.className).toContain('is-danger');
});

test('defaults to strip variant (bare b-btn)', () => {
  render(<Button>Refresh</Button>);
  expect(screen.getByRole('button', { name: 'Refresh' }).className.trim()).toBe('b-btn');
});
```

- [ ] **Step 7: Run test to verify it fails**

Run: `cd design-system/react && npm install && npx vitest --run`
Expected: FAIL — cannot resolve `../Button`.

- [ ] **Step 8: Write `design-system/react/src/components/Button.tsx`**

```tsx
import type { ButtonHTMLAttributes, ReactNode } from 'react';

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  /** 'strip' = card action-strip button (default); 'primary' = gold solid with shine; 'ghost' = glass outline */
  variant?: 'strip' | 'primary' | 'ghost';
  /** danger tone (red hover/border) */
  danger?: boolean;
  children: ReactNode;
}

export function Button({ variant = 'strip', danger = false, className = '', children, ...rest }: ButtonProps) {
  const cls = [
    'b-btn',
    variant === 'primary' ? 'is-primary' : '',
    variant === 'ghost' ? 'is-ghost' : '',
    danger ? 'is-danger' : '',
    className,
  ].filter(Boolean).join(' ');
  return (
    <button className={cls} {...rest}>
      {children}
    </button>
  );
}
```

- [ ] **Step 9: Write `design-system/react/src/index.ts`**

```ts
import './styles.css';

export { Button } from './components/Button';
export type { ButtonProps } from './components/Button';
```

- [ ] **Step 10: Write `design-system/react/.gitignore`**

```
node_modules/
dist/
```

- [ ] **Step 11: Run tests + build to verify green**

Run: `cd design-system/react && npx vitest --run && npx tsc --noEmit && npm run build && ls dist/index.js dist/index.css dist/index.d.ts`
Expected: 3 tests pass, tsc clean, all three dist files listed.

- [ ] **Step 12: Commit**

```bash
git add design-system/react/
git commit -m "feat(design-system): @vigilant/ui scaffold with esbuild pipeline and Button"
```

---

### Task 6: Layout components

**Goal:** NavBar, NavMenu, Breadcrumbs, PageHeader, Section, Panel, Grid, TabStrip, Footer — with tests.

**Files:**
- Create: `design-system/react/src/components/NavBar.tsx`
- Create: `design-system/react/src/components/NavMenu.tsx`
- Create: `design-system/react/src/components/Breadcrumbs.tsx`
- Create: `design-system/react/src/components/PageHeader.tsx`
- Create: `design-system/react/src/components/Section.tsx`
- Create: `design-system/react/src/components/Panel.tsx`
- Create: `design-system/react/src/components/Grid.tsx`
- Create: `design-system/react/src/components/TabStrip.tsx`
- Create: `design-system/react/src/components/Footer.tsx`
- Create: `design-system/react/src/components/__tests__/layout.test.tsx`
- Modify: `design-system/react/src/index.ts` (add exports)

**Acceptance Criteria:**
- [ ] All 9 components render their `b-*` classes; each exports `<Name>` + `<Name>Props`
- [ ] Panel supports `glass` and `brackets` boolean props → `is-glass` / `is-brackets`
- [ ] NavMenu renders a `b-nav-dropdown` with items (hover/focus behavior is pure CSS)
- [ ] `npx vitest --run` green, `npx tsc --noEmit` clean

**Verify:** `cd design-system/react && npx vitest --run && npx tsc --noEmit` → all pass

**Steps:**

- [ ] **Step 1: Write the failing test `design-system/react/src/components/__tests__/layout.test.tsx`**

```tsx
import { render, screen } from '@testing-library/react';
import { NavBar } from '../NavBar';
import { NavMenu } from '../NavMenu';
import { Breadcrumbs } from '../Breadcrumbs';
import { PageHeader } from '../PageHeader';
import { Section } from '../Section';
import { Panel } from '../Panel';
import { Grid } from '../Grid';
import { TabStrip } from '../TabStrip';
import { Footer } from '../Footer';

test('NavBar renders logo and children', () => {
  const { container } = render(
    <NavBar logo="VIGILANT" logoHref="/">
      <a className="b-nav-link" href="/intel">Intel</a>
    </NavBar>
  );
  expect(container.querySelector('.b-nav')).toBeTruthy();
  expect(screen.getByText('VIGILANT').className).toContain('b-nav-logo');
});

test('NavMenu renders dropdown items', () => {
  const { container } = render(
    <NavMenu label="Intel" items={[
      { label: 'Kill Feed', href: '/intel/kills', active: true },
      { label: 'D-Scan', href: '/intel/dscan' },
    ]} />
  );
  expect(container.querySelector('.b-nav-dropdown-menu')).toBeTruthy();
  expect(screen.getByText('Kill Feed').className).toContain('is-active');
});

test('Breadcrumbs renders crumbs with separators and current', () => {
  const { container } = render(
    <Breadcrumbs crumbs={[{ label: 'Intel', href: '/intel' }, { label: 'Kills' }]} />
  );
  expect(container.querySelectorAll('.b-crumb-sep')).toHaveLength(1);
  expect(screen.getByText('Kills').className).toContain('b-crumb-current');
});

test('PageHeader renders title and actions', () => {
  render(<PageHeader title="Dashboard" actions={<button>Refresh</button>} />);
  expect(screen.getByText('Dashboard').className).toContain('b-page-title');
  expect(screen.getByRole('button', { name: 'Refresh' })).toBeTruthy();
});

test('Section renders head label and children', () => {
  render(<Section title="Recent Kills"><p>rows</p></Section>);
  expect(screen.getByText('Recent Kills').className).toContain('b-label');
  expect(screen.getByText('rows')).toBeTruthy();
});

test('Panel glass + brackets modifiers', () => {
  const { container } = render(<Panel title="Fleet" glass brackets>body</Panel>);
  const el = container.querySelector('.b-panel')!;
  expect(el.className).toContain('is-glass');
  expect(el.className).toContain('is-brackets');
});

test('Grid cols 2 and 3', () => {
  const g2 = render(<Grid cols={2}>x</Grid>).container.firstElementChild!;
  const g3 = render(<Grid cols={3}>x</Grid>).container.firstElementChild!;
  expect(g2.className).toContain('b-grid-2');
  expect(g3.className).toContain('b-grid-3');
});

test('TabStrip active tab + onSelect', () => {
  const onSelect = vi.fn();
  render(<TabStrip tabs={[{ label: 'Alpha', active: true }, { label: 'Beta' }]} onSelect={onSelect} />);
  expect(screen.getByText('Alpha').className).toContain('is-active');
  screen.getByText('Beta').click();
  expect(onSelect).toHaveBeenCalledWith(1);
});

test('Footer renders links and brand', () => {
  render(<Footer links={[{ label: 'GitHub', href: 'https://github.com' }]} brand="THUNDERBORN" />);
  expect(screen.getByText('GitHub').className).toContain('b-footer-link');
  expect(screen.getByText('THUNDERBORN')).toBeTruthy();
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd design-system/react && npx vitest --run`
Expected: FAIL — modules not found.

- [ ] **Step 3: Write the nine components**

`NavBar.tsx`:
```tsx
import type { ReactNode } from 'react';

export interface NavBarProps {
  logo: string;
  logoHref?: string;
  /** nav links / NavMenu elements */
  children?: ReactNode;
  /** right-aligned content (auth state, server bar, etc.) */
  right?: ReactNode;
}

export function NavBar({ logo, logoHref = '/', children, right }: NavBarProps) {
  return (
    <nav className="b-nav">
      <a className="b-nav-logo" href={logoHref}>{logo}</a>
      <div className="b-nav-links">{children}</div>
      {right ? <div className="b-nav-links">{right}</div> : null}
    </nav>
  );
}
```

`NavMenu.tsx`:
```tsx
export interface NavMenuItem {
  label: string;
  href: string;
  active?: boolean;
}

export interface NavMenuProps {
  label: string;
  items: NavMenuItem[];
  active?: boolean;
}

export function NavMenu({ label, items, active = false }: NavMenuProps) {
  return (
    <div className="b-nav-dropdown">
      <a className={`b-nav-link${active ? ' is-active' : ''}`} href={items[0]?.href ?? '#'}>
        {label} ▾
      </a>
      <div className="b-nav-dropdown-menu">
        {items.map((it) => (
          <a key={it.href} className={`b-nav-dropdown-item${it.active ? ' is-active' : ''}`} href={it.href}>
            {it.label}
          </a>
        ))}
      </div>
    </div>
  );
}
```

`Breadcrumbs.tsx`:
```tsx
import { Fragment } from 'react';

export interface Crumb {
  label: string;
  href?: string;
}

export interface BreadcrumbsProps {
  crumbs: Crumb[];
}

export function Breadcrumbs({ crumbs }: BreadcrumbsProps) {
  return (
    <div className="b-breadcrumbs">
      {crumbs.map((c, i) => (
        <Fragment key={i}>
          {i > 0 && <span className="b-crumb-sep">/</span>}
          {c.href ? <a href={c.href}>{c.label}</a> : <span className="b-crumb-current">{c.label}</span>}
        </Fragment>
      ))}
    </div>
  );
}
```

`PageHeader.tsx`:
```tsx
import type { ReactNode } from 'react';

export interface PageHeaderProps {
  title: string;
  actions?: ReactNode;
}

export function PageHeader({ title, actions }: PageHeaderProps) {
  return (
    <div className="b-page-header">
      <h1 className="b-page-title">{title}</h1>
      {actions ? <div>{actions}</div> : null}
    </div>
  );
}
```

`Section.tsx`:
```tsx
import type { ReactNode } from 'react';

export interface SectionProps {
  title: string;
  actions?: ReactNode;
  children: ReactNode;
}

export function Section({ title, actions, children }: SectionProps) {
  return (
    <section className="b-section">
      <div className="b-section-head">
        <span className="b-label">{title}</span>
        {actions ? <div>{actions}</div> : null}
      </div>
      {children}
    </section>
  );
}
```

`Panel.tsx`:
```tsx
import type { ReactNode } from 'react';

export interface PanelProps {
  title?: string;
  actions?: ReactNode;
  /** translucent glass surface with blur */
  glass?: boolean;
  /** gold corner brackets */
  brackets?: boolean;
  children: ReactNode;
}

export function Panel({ title, actions, glass = false, brackets = false, children }: PanelProps) {
  const cls = ['b-panel', glass ? 'is-glass' : '', brackets ? 'is-brackets' : ''].filter(Boolean).join(' ');
  return (
    <div className={cls}>
      {title ? (
        <div className="b-panel-head">
          <span className="b-label">{title}</span>
          {actions ? <div>{actions}</div> : null}
        </div>
      ) : null}
      {children}
    </div>
  );
}
```

`Grid.tsx`:
```tsx
import type { ReactNode } from 'react';

export interface GridProps {
  cols: 2 | 3;
  children: ReactNode;
}

export function Grid({ cols, children }: GridProps) {
  return <div className={cols === 3 ? 'b-grid-3' : 'b-grid-2'}>{children}</div>;
}
```

`TabStrip.tsx`:
```tsx
export interface Tab {
  label: string;
  href?: string;
  active?: boolean;
}

export interface TabStripProps {
  tabs: Tab[];
  /** called with the tab index when a non-link tab is clicked */
  onSelect?: (index: number) => void;
}

export function TabStrip({ tabs, onSelect }: TabStripProps) {
  return (
    <div className="b-tab-strip">
      {tabs.map((tab, i) =>
        tab.href ? (
          <a key={i} href={tab.href} className={tab.active ? 'is-active' : ''}>{tab.label}</a>
        ) : (
          <button key={i} type="button" className={tab.active ? 'is-active' : ''} onClick={() => onSelect?.(i)}>
            {tab.label}
          </button>
        )
      )}
    </div>
  );
}
```

`Footer.tsx`:
```tsx
export interface FooterLink {
  label: string;
  href: string;
}

export interface FooterProps {
  links?: FooterLink[];
  brand?: string;
}

export function Footer({ links = [], brand }: FooterProps) {
  return (
    <footer className="b-footer">
      <div className="b-footer-links">
        {links.map((l) => (
          <a key={l.href} className="b-footer-link" href={l.href}>{l.label}</a>
        ))}
      </div>
      {brand ? <span className="b-footer-brand">{brand}</span> : null}
    </footer>
  );
}
```

- [ ] **Step 4: Add exports to `design-system/react/src/index.ts`**

```ts
export { NavBar } from './components/NavBar';
export type { NavBarProps } from './components/NavBar';
export { NavMenu } from './components/NavMenu';
export type { NavMenuProps, NavMenuItem } from './components/NavMenu';
export { Breadcrumbs } from './components/Breadcrumbs';
export type { BreadcrumbsProps, Crumb } from './components/Breadcrumbs';
export { PageHeader } from './components/PageHeader';
export type { PageHeaderProps } from './components/PageHeader';
export { Section } from './components/Section';
export type { SectionProps } from './components/Section';
export { Panel } from './components/Panel';
export type { PanelProps } from './components/Panel';
export { Grid } from './components/Grid';
export type { GridProps } from './components/Grid';
export { TabStrip } from './components/TabStrip';
export type { TabStripProps, Tab } from './components/TabStrip';
export { Footer } from './components/Footer';
export type { FooterProps, FooterLink } from './components/Footer';
```

- [ ] **Step 5: Run tests to verify green**

Run: `cd design-system/react && npx vitest --run && npx tsc --noEmit`
Expected: all tests pass, tsc clean.

- [ ] **Step 6: Commit**

```bash
git add design-system/react/src/
git commit -m "feat(design-system): layout components (NavBar, NavMenu, Panel, Section, Grid, TabStrip, Breadcrumbs, PageHeader, Footer)"
```

---

### Task 7: Data components

**Goal:** StatStrip/StatBlock, KeyValueRow, Table/TableRow, Badge, ProgressBar, EmptyState, Eyebrow — with tests.

**Files:**
- Create: `design-system/react/src/components/Stat.tsx` (exports StatStrip + StatBlock)
- Create: `design-system/react/src/components/KeyValueRow.tsx`
- Create: `design-system/react/src/components/Table.tsx` (exports Table + TableRow)
- Create: `design-system/react/src/components/Badge.tsx`
- Create: `design-system/react/src/components/ProgressBar.tsx`
- Create: `design-system/react/src/components/EmptyState.tsx`
- Create: `design-system/react/src/components/Eyebrow.tsx`
- Create: `design-system/react/src/components/tones.ts` (shared Tone type)
- Create: `design-system/react/src/components/__tests__/data.test.tsx`
- Modify: `design-system/react/src/index.ts` (add exports)

**Acceptance Criteria:**
- [ ] Shared `Tone` type: `'default' | 'accent' | 'ok' | 'warn' | 'danger' | 'muted'` in `tones.ts`, mapped to `is-*` classes by one helper used by all tone-aware components
- [ ] `ProgressBar` clamps `value` to 0–100 and maps `tone` to `is-active`/`is-warn`/`is-crit`
- [ ] `Table` wraps rows in a `b-panel`; `TableRow` renders `b-table-row` with `stagger` support via parent `vg-stagger` class on Table
- [ ] `npx vitest --run` green, `npx tsc --noEmit` clean

**Verify:** `cd design-system/react && npx vitest --run && npx tsc --noEmit` → all pass

**Steps:**

- [ ] **Step 1: Write `design-system/react/src/components/tones.ts`**

```ts
export type Tone = 'default' | 'accent' | 'ok' | 'warn' | 'danger' | 'muted';

export function toneClass(tone: Tone | undefined): string {
  switch (tone) {
    case 'accent': return 'is-accent';
    case 'ok': return 'is-ok';
    case 'warn': return 'is-warn';
    case 'danger': return 'is-danger';
    case 'muted': return 'is-muted';
    default: return '';
  }
}
```

- [ ] **Step 2: Write the failing test `design-system/react/src/components/__tests__/data.test.tsx`**

```tsx
import { render, screen } from '@testing-library/react';
import { StatStrip, StatBlock } from '../Stat';
import { KeyValueRow } from '../KeyValueRow';
import { Table, TableRow } from '../Table';
import { Badge } from '../Badge';
import { ProgressBar } from '../ProgressBar';
import { EmptyState } from '../EmptyState';
import { Eyebrow } from '../Eyebrow';

test('StatStrip + StatBlock render values with tones', () => {
  const { container } = render(
    <StatStrip>
      <StatBlock label="Wallet" value="4.2B ISK" />
      <StatBlock label="Alerts" value="2" tone="danger" />
    </StatStrip>
  );
  expect(container.querySelector('.b-stats')).toBeTruthy();
  expect(screen.getByText('2').className).toContain('is-danger');
  expect(screen.getByText('WALLET') || screen.getByText('Wallet')).toBeTruthy();
});

test('KeyValueRow renders label/value with tone', () => {
  render(<KeyValueRow label="Fuel" value="42 days" tone="warn" />);
  expect(screen.getByText('Fuel').className).toContain('b-row-label');
  expect(screen.getByText('42 days').className).toContain('is-warn');
});

test('Table renders rows in a panel with stagger', () => {
  const { container } = render(
    <Table stagger>
      <TableRow><span>Loki</span><span>+412M</span></TableRow>
      <TableRow><span>Drake</span><span>−86M</span></TableRow>
    </Table>
  );
  expect(container.querySelector('.b-panel')).toBeTruthy();
  expect(container.querySelector('.vg-stagger')).toBeTruthy();
  expect(container.querySelectorAll('.b-table-row')).toHaveLength(2);
});

test('Badge tones and active', () => {
  render(<Badge tone="danger">HOSTILE</Badge>);
  expect(screen.getByText('HOSTILE').className).toContain('is-danger');
  render(<Badge active>ONLINE</Badge>);
  expect(screen.getByText('ONLINE').className).toContain('is-active');
});

test('ProgressBar clamps and maps tone', () => {
  const { container } = render(<ProgressBar value={150} tone="danger" />);
  const fill = container.querySelector('.b-progress-fill') as HTMLElement;
  expect(fill.style.width).toBe('100%');
  expect(fill.className).toContain('is-crit');
});

test('EmptyState and Eyebrow', () => {
  render(<EmptyState>No kills recorded</EmptyState>);
  expect(screen.getByText('No kills recorded').className).toContain('b-empty');
  render(<Eyebrow>Intel</Eyebrow>);
  expect(screen.getByText('Intel').className).toContain('b-eyebrow');
});
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd design-system/react && npx vitest --run`
Expected: FAIL — modules not found.

- [ ] **Step 4: Write the components**

`Stat.tsx`:
```tsx
import type { ReactNode } from 'react';
import { toneClass, type Tone } from './tones';

export interface StatStripProps {
  children: ReactNode;
}

export function StatStrip({ children }: StatStripProps) {
  return <div className="b-stats">{children}</div>;
}

export interface StatBlockProps {
  label: string;
  value: ReactNode;
  tone?: Tone;
}

export function StatBlock({ label, value, tone }: StatBlockProps) {
  return (
    <div className="b-stat">
      <div className={`b-stat-val ${toneClass(tone)}`.trim()}>{value}</div>
      <div className="b-stat-label">{label}</div>
    </div>
  );
}
```

`KeyValueRow.tsx`:
```tsx
import type { ReactNode } from 'react';
import { toneClass, type Tone } from './tones';

export interface KeyValueRowProps {
  label: string;
  value: ReactNode;
  tone?: Tone;
}

export function KeyValueRow({ label, value, tone }: KeyValueRowProps) {
  return (
    <div className="b-row">
      <span className="b-row-label">{label}</span>
      <span className={`b-row-val ${toneClass(tone)}`.trim()}>{value}</span>
    </div>
  );
}
```

`Table.tsx`:
```tsx
import type { ReactNode } from 'react';

export interface TableProps {
  /** optional b-panel-head title */
  title?: string;
  /** staggered row entrance animation */
  stagger?: boolean;
  children: ReactNode;
}

export function Table({ title, stagger = false, children }: TableProps) {
  return (
    <div className="b-panel">
      {title ? (
        <div className="b-panel-head"><span className="b-label">{title}</span></div>
      ) : null}
      <div className={stagger ? 'vg-stagger' : undefined}>{children}</div>
    </div>
  );
}

export interface TableRowProps {
  children: ReactNode;
  onClick?: () => void;
}

export function TableRow({ children, onClick }: TableRowProps) {
  return (
    <div className="b-table-row" onClick={onClick} role={onClick ? 'button' : undefined}>
      {children}
    </div>
  );
}
```

`Badge.tsx`:
```tsx
import type { ReactNode } from 'react';
import { toneClass, type Tone } from './tones';

export interface BadgeProps {
  tone?: Tone;
  /** inverted (filled) style */
  active?: boolean;
  children: ReactNode;
}

export function Badge({ tone, active = false, children }: BadgeProps) {
  const cls = ['b-badge', active ? 'is-active' : toneClass(tone)].filter(Boolean).join(' ');
  return <span className={cls}>{children}</span>;
}
```

`ProgressBar.tsx`:
```tsx
export interface ProgressBarProps {
  /** 0–100; values outside the range are clamped */
  value: number;
  tone?: 'default' | 'active' | 'warn' | 'danger';
}

export function ProgressBar({ value, tone = 'default' }: ProgressBarProps) {
  const pct = Math.max(0, Math.min(100, value));
  const cls = ['b-progress-fill', tone === 'active' ? 'is-active' : '', tone === 'warn' ? 'is-warn' : '', tone === 'danger' ? 'is-crit' : ''].filter(Boolean).join(' ');
  return (
    <div className="b-progress">
      <div className={cls} style={{ width: `${pct}%` }} />
    </div>
  );
}
```

`EmptyState.tsx`:
```tsx
import type { ReactNode } from 'react';

export interface EmptyStateProps {
  children: ReactNode;
}

export function EmptyState({ children }: EmptyStateProps) {
  return <div className="b-empty">{children}</div>;
}
```

`Eyebrow.tsx`:
```tsx
import type { ReactNode } from 'react';

export interface EyebrowProps {
  children: ReactNode;
}

export function Eyebrow({ children }: EyebrowProps) {
  return <span className="b-eyebrow">{children}</span>;
}
```

- [ ] **Step 5: Add exports to `design-system/react/src/index.ts`**

```ts
export { StatStrip, StatBlock } from './components/Stat';
export type { StatStripProps, StatBlockProps } from './components/Stat';
export { KeyValueRow } from './components/KeyValueRow';
export type { KeyValueRowProps } from './components/KeyValueRow';
export { Table, TableRow } from './components/Table';
export type { TableProps, TableRowProps } from './components/Table';
export { Badge } from './components/Badge';
export type { BadgeProps } from './components/Badge';
export { ProgressBar } from './components/ProgressBar';
export type { ProgressBarProps } from './components/ProgressBar';
export { EmptyState } from './components/EmptyState';
export type { EmptyStateProps } from './components/EmptyState';
export { Eyebrow } from './components/Eyebrow';
export type { EyebrowProps } from './components/Eyebrow';
export type { Tone } from './components/tones';
```

- [ ] **Step 6: Run tests to verify green**

Run: `cd design-system/react && npx vitest --run && npx tsc --noEmit`
Expected: all tests pass, tsc clean.

- [ ] **Step 7: Commit**

```bash
git add design-system/react/src/
git commit -m "feat(design-system): data components (Stat, KeyValueRow, Table, Badge, ProgressBar, EmptyState, Eyebrow)"
```

---

### Task 8: Actions + feedback components

**Goal:** ButtonGroup, Banner, Toast/ToastStack, Modal, Skeleton — with tests.

**Files:**
- Create: `design-system/react/src/components/ButtonGroup.tsx`
- Create: `design-system/react/src/components/Banner.tsx`
- Create: `design-system/react/src/components/Toast.tsx` (exports Toast + ToastStack)
- Create: `design-system/react/src/components/Modal.tsx`
- Create: `design-system/react/src/components/Skeleton.tsx`
- Create: `design-system/react/src/components/__tests__/feedback.test.tsx`
- Modify: `design-system/react/src/index.ts` (add exports)

**Acceptance Criteria:**
- [ ] Modal renders nothing when `open={false}`; renders overlay + `b-modal` with title head and close button when open; calls `onClose` on close click, overlay click, and Escape key
- [ ] Banner supports tones (`is-danger`/`is-ok`) and optional dismiss
- [ ] Toast is presentational (`b-toast` + tone); ToastStack is the fixed positioner
- [ ] Skeleton renders `lines` skeleton bars (default 3)
- [ ] `npx vitest --run` green, `npx tsc --noEmit` clean

**Verify:** `cd design-system/react && npx vitest --run && npx tsc --noEmit` → all pass

**Steps:**

- [ ] **Step 1: Write the failing test `design-system/react/src/components/__tests__/feedback.test.tsx`**

```tsx
import { render, screen, fireEvent } from '@testing-library/react';
import { ButtonGroup } from '../ButtonGroup';
import { Button } from '../Button';
import { Banner } from '../Banner';
import { Toast, ToastStack } from '../Toast';
import { Modal } from '../Modal';
import { Skeleton } from '../Skeleton';

test('ButtonGroup renders b-actions strip', () => {
  const { container } = render(
    <ButtonGroup><Button>View</Button><Button danger>Delete</Button></ButtonGroup>
  );
  expect(container.querySelector('.b-actions')).toBeTruthy();
});

test('Banner tone + dismiss', () => {
  const onDismiss = vi.fn();
  render(<Banner tone="danger" onDismiss={onDismiss}>Structure under attack</Banner>);
  const banner = screen.getByText('Structure under attack').closest('.b-banner') as HTMLElement;
  expect(banner.className).toContain('is-danger');
  fireEvent.click(screen.getByRole('button', { name: '×' }));
  expect(onDismiss).toHaveBeenCalled();
});

test('ToastStack positions toasts with tones', () => {
  const { container } = render(
    <ToastStack>
      <Toast tone="ok">Saved</Toast>
      <Toast tone="danger">Failed</Toast>
    </ToastStack>
  );
  expect(container.querySelector('.b-toast-stack')).toBeTruthy();
  expect(screen.getByText('Failed').closest('.b-toast')!.className).toContain('is-danger');
});

test('Modal hidden when closed, interactive when open', () => {
  const onClose = vi.fn();
  const { rerender, container } = render(<Modal open={false} title="Confirm" onClose={onClose}>body</Modal>);
  expect(container.querySelector('.b-modal')).toBeNull();
  rerender(<Modal open title="Confirm" onClose={onClose}>body</Modal>);
  expect(container.querySelector('.b-modal')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: '×' }));
  fireEvent.keyDown(document, { key: 'Escape' });
  expect(onClose).toHaveBeenCalledTimes(2);
});

test('Skeleton renders n lines', () => {
  const { container } = render(<Skeleton lines={4} />);
  expect(container.querySelectorAll('.b-skeleton')).toHaveLength(4);
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd design-system/react && npx vitest --run`
Expected: FAIL — modules not found.

- [ ] **Step 3: Write the components**

`ButtonGroup.tsx`:
```tsx
import type { ReactNode } from 'react';

export interface ButtonGroupProps {
  children: ReactNode;
}

export function ButtonGroup({ children }: ButtonGroupProps) {
  return <div className="b-actions">{children}</div>;
}
```

`Banner.tsx`:
```tsx
import type { ReactNode } from 'react';

export interface BannerProps {
  tone?: 'accent' | 'danger' | 'ok';
  onDismiss?: () => void;
  children: ReactNode;
}

export function Banner({ tone = 'accent', onDismiss, children }: BannerProps) {
  const cls = ['b-banner', tone === 'danger' ? 'is-danger' : '', tone === 'ok' ? 'is-ok' : ''].filter(Boolean).join(' ');
  return (
    <div className={cls}>
      <span>{children}</span>
      {onDismiss ? (
        <button type="button" className="b-modal-close" onClick={onDismiss} aria-label="×">×</button>
      ) : null}
    </div>
  );
}
```

`Toast.tsx`:
```tsx
import type { ReactNode } from 'react';

export interface ToastProps {
  tone?: 'accent' | 'ok' | 'danger' | 'info';
  children: ReactNode;
}

export function Toast({ tone = 'accent', children }: ToastProps) {
  const cls = ['b-toast', tone === 'ok' ? 'is-ok' : '', tone === 'danger' ? 'is-danger' : '', tone === 'info' ? 'is-info' : ''].filter(Boolean).join(' ');
  return <div className={cls}>{children}</div>;
}

export interface ToastStackProps {
  children: ReactNode;
}

export function ToastStack({ children }: ToastStackProps) {
  return <div className="b-toast-stack">{children}</div>;
}
```

`Modal.tsx`:
```tsx
import { useEffect, type ReactNode } from 'react';

export interface ModalProps {
  open: boolean;
  title: string;
  onClose: () => void;
  children: ReactNode;
}

export function Modal({ open, title, onClose, children }: ModalProps) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  if (!open) return null;
  return (
    <div className="b-modal-overlay" onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="b-modal" role="dialog" aria-label={title}>
        <div className="b-modal-head">
          <span className="b-label">{title}</span>
          <button type="button" className="b-modal-close" onClick={onClose} aria-label="×">×</button>
        </div>
        <div className="b-modal-body">{children}</div>
      </div>
    </div>
  );
}
```

`Skeleton.tsx`:
```tsx
export interface SkeletonProps {
  /** number of shimmer bars (default 3) */
  lines?: number;
  /** CSS width of the last line, e.g. '60%' */
  lastLineWidth?: string;
}

export function Skeleton({ lines = 3, lastLineWidth = '60%' }: SkeletonProps) {
  return (
    <div>
      {Array.from({ length: lines }, (_, i) => (
        <div key={i} className="b-skeleton" style={i === lines - 1 ? { width: lastLineWidth } : undefined} />
      ))}
    </div>
  );
}
```

- [ ] **Step 4: Add exports to `design-system/react/src/index.ts`**

```ts
export { ButtonGroup } from './components/ButtonGroup';
export type { ButtonGroupProps } from './components/ButtonGroup';
export { Banner } from './components/Banner';
export type { BannerProps } from './components/Banner';
export { Toast, ToastStack } from './components/Toast';
export type { ToastProps, ToastStackProps } from './components/Toast';
export { Modal } from './components/Modal';
export type { ModalProps } from './components/Modal';
export { Skeleton } from './components/Skeleton';
export type { SkeletonProps } from './components/Skeleton';
```

- [ ] **Step 5: Run tests to verify green**

Run: `cd design-system/react && npx vitest --run && npx tsc --noEmit`
Expected: all tests pass, tsc clean.

- [ ] **Step 6: Commit**

```bash
git add design-system/react/src/
git commit -m "feat(design-system): action and feedback components (ButtonGroup, Banner, Toast, Modal, Skeleton)"
```

---

### Task 9: AmbientBackground wrapper + fonts

**Goal:** React wrapper for the ambient module, plus bundled JetBrains Mono woff2 fonts.

**Files:**
- Create: `design-system/react/src/components/AmbientBackground.tsx`
- Create: `design-system/react/src/components/__tests__/ambient.test.tsx`
- Create: `design-system/react/src/fonts.css`
- Create: `design-system/react/fonts/` (woff2 files copied from @fontsource)
- Modify: `design-system/react/src/styles.css` (add fonts import)
- Modify: `design-system/react/src/index.ts` (add exports)

**Acceptance Criteria:**
- [ ] `AmbientBackground` mounts the module on mount, destroys on unmount; accepts and forwards `systemsUrl`, `killSource`, `minWidth`, `fpsCap`
- [ ] Fonts: latin woff2 for weights 300/400/600 in `design-system/react/fonts/`, `@font-face` rules in `fonts.css`, imported from `styles.css`
- [ ] `npm run build` still succeeds with fonts bundled (`dist/` contains woff2 files)
- [ ] Tests green, tsc clean

**Verify:** `cd design-system/react && npx vitest --run && npx tsc --noEmit && npm run build && ls dist/*.woff2 | head -1` → tests pass, at least one woff2 in dist

**Steps:**

- [ ] **Step 1: Write the failing test `design-system/react/src/components/__tests__/ambient.test.tsx`**

The module is mocked — jsdom has no canvas:

```tsx
import { render, cleanup } from '@testing-library/react';

const destroy = vi.fn();
const mount = vi.fn(() => ({ destroy }));
vi.mock('../../../../ambient/vigilant-ambient.js', () => ({ mount }));

import { AmbientBackground } from '../AmbientBackground';

test('mounts ambient module and destroys on unmount', () => {
  render(<AmbientBackground systemsUrl="/data/systems.json" killSource={{ type: 'simulate' }} />);
  expect(mount).toHaveBeenCalledTimes(1);
  expect(mount.mock.calls[0][1]).toMatchObject({ systemsUrl: '/data/systems.json' });
  cleanup();
  expect(destroy).toHaveBeenCalledTimes(1);
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd design-system/react && npx vitest --run`
Expected: FAIL — `../AmbientBackground` not found.

- [ ] **Step 3: Write `design-system/react/src/components/AmbientBackground.tsx`**

```tsx
import { useEffect, useRef } from 'react';
import { mount, type AmbientOptions } from '../../../ambient/vigilant-ambient.js';

export interface AmbientBackgroundProps extends AmbientOptions {
  className?: string;
}

export function AmbientBackground({ className, ...options }: AmbientBackgroundProps) {
  const ref = useRef<HTMLDivElement>(null);
  const optionsRef = useRef(options);
  optionsRef.current = options;

  useEffect(() => {
    if (!ref.current) return;
    const handle = mount(ref.current, optionsRef.current);
    return () => handle.destroy();
  }, []);

  return <div ref={ref} className={className} aria-hidden="true" />;
}
```

Note: the import path resolves because `tsconfig.json` includes `../ambient/vigilant-ambient.d.ts` and esbuild bundles the `.js` relative import. If tsc complains about the declaration pairing, add `"paths"` mapping or a `src/ambient.d.ts` with `declare module '../../../ambient/vigilant-ambient.js' { export * from '../../ambient/vigilant-ambient'; }` — prefer whichever keeps `tsc --noEmit` clean without weakening types.

- [ ] **Step 4: Fonts — install and copy**

Run:
```bash
cd design-system/react
npm install
mkdir -p fonts
cp node_modules/@fontsource/jetbrains-mono/files/jetbrains-mono-latin-300-normal.woff2 fonts/
cp node_modules/@fontsource/jetbrains-mono/files/jetbrains-mono-latin-400-normal.woff2 fonts/
cp node_modules/@fontsource/jetbrains-mono/files/jetbrains-mono-latin-600-normal.woff2 fonts/
```

- [ ] **Step 5: Write `design-system/react/src/fonts.css`**

```css
@font-face { font-family: 'JetBrains Mono'; font-style: normal; font-weight: 300; font-display: swap; src: url('../fonts/jetbrains-mono-latin-300-normal.woff2') format('woff2'); }
@font-face { font-family: 'JetBrains Mono'; font-style: normal; font-weight: 400; font-display: swap; src: url('../fonts/jetbrains-mono-latin-400-normal.woff2') format('woff2'); }
@font-face { font-family: 'JetBrains Mono'; font-style: normal; font-weight: 600; font-display: swap; src: url('../fonts/jetbrains-mono-latin-600-normal.woff2') format('woff2'); }
```

- [ ] **Step 6: Update `design-system/react/src/styles.css`**

```css
@import "./fonts.css";
@import "../../css/tokens.css";
@import "../../css/motion.css";
@import "../../css/components.css";
```

- [ ] **Step 7: Add exports to `design-system/react/src/index.ts`**

```ts
export { AmbientBackground } from './components/AmbientBackground';
export type { AmbientBackgroundProps } from './components/AmbientBackground';
```

- [ ] **Step 8: Run tests + build to verify green**

Run: `cd design-system/react && npx vitest --run && npx tsc --noEmit && npm run build && ls dist/ | grep -c woff2`
Expected: tests pass, tsc clean, build emits ≥3 woff2 files.

- [ ] **Step 9: Commit**

```bash
git add design-system/react/
git commit -m "feat(design-system): AmbientBackground wrapper and bundled JetBrains Mono fonts"
```

---

### Task 10: Demo harness

**Goal:** A Vite page rendering every component with realistic EVE content over the live ambient background — the visual verification surface and the usage-example source for design-sync.

**Files:**
- Create: `design-system/react/demo/index.html`
- Create: `design-system/react/demo/main.tsx`
- Create: `design-system/react/demo/vite.config.ts`
- Create: `design-system/react/demo/public/data/systems.json` (copied)

**Acceptance Criteria:**
- [ ] `npx vite build demo` succeeds
- [ ] Demo renders ALL exported components at least once with realistic EVE content (ISK values, system names, ship types)
- [ ] AmbientBackground runs with `killSource: {type:'simulate'}` and the copied systems.json
- [ ] User has eyeballed the demo (`npm run demo`) and approved the look — this is a REVIEW GATE; pause execution and ask

**Verify:** `cd design-system/react && npx vite build demo && ls demo/dist/index.html` → build succeeds; then human review via `npm run demo`

**Steps:**

- [ ] **Step 1: Copy the systems data**

```bash
mkdir -p design-system/react/demo/public/data
cp frontend/public/data/systems.json design-system/react/demo/public/data/systems.json
```

- [ ] **Step 2: Write `design-system/react/demo/vite.config.ts`**

```ts
import { defineConfig } from 'vite';

export default defineConfig({
  root: __dirname,
  esbuild: { jsx: 'automatic' },
});
```

- [ ] **Step 3: Write `design-system/react/demo/index.html`**

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>@vigilant/ui — component demo</title>
</head>
<body>
  <div id="root"></div>
  <script type="module" src="/main.tsx"></script>
</body>
</html>
```

- [ ] **Step 4: Write `design-system/react/demo/main.tsx`**

```tsx
import { createRoot } from 'react-dom/client';
import { useState } from 'react';
import {
  AmbientBackground, NavBar, NavMenu, Breadcrumbs, PageHeader, Section, Panel,
  Grid, TabStrip, Footer, StatStrip, StatBlock, KeyValueRow, Table, TableRow,
  Badge, ProgressBar, EmptyState, Eyebrow, Button, ButtonGroup, Banner,
  Toast, ToastStack, Modal, Skeleton,
} from '../src/index';

function Demo() {
  const [modalOpen, setModalOpen] = useState(false);
  const [bannerVisible, setBannerVisible] = useState(true);
  return (
    <>
      <AmbientBackground systemsUrl="/data/systems.json" killSource={{ type: 'simulate' }} minWidth={0} />
      <NavBar logo="VIGILANT" right={<a className="b-nav-link" href="#">LOGOUT</a>}>
        <NavMenu label="Intel" active items={[
          { label: 'Kill Feed', href: '#kills', active: true },
          { label: 'D-Scan', href: '#dscan' },
          { label: 'Local Watch', href: '#local' },
        ]} />
        <NavMenu label="Industry" items={[
          { label: 'Jobs', href: '#jobs' },
          { label: 'Blueprints', href: '#bp' },
        ]} />
        <a className="b-nav-link" href="#map">Map</a>
      </NavBar>
      <Breadcrumbs crumbs={[{ label: 'Home', href: '#' }, { label: 'Intel', href: '#' }, { label: 'Demo' }]} />
      <main className="b-main">
        <PageHeader title="Component Demo" actions={<Button variant="primary" onClick={() => setModalOpen(true)}>Open Modal</Button>} />
        {bannerVisible && (
          <Banner tone="danger" onDismiss={() => setBannerVisible(false)}>
            Structure ALERT — Thunderborn HQ armor timer in 3h 12m
          </Banner>
        )}
        <StatStrip>
          <StatBlock label="Wallet" value="4.2B ISK" />
          <StatBlock label="Skill Queue" value="3D 14H" tone="accent" />
          <StatBlock label="Alerts" value="2" tone="danger" />
          <StatBlock label="Fleet" value="ONLINE" tone="ok" />
        </StatStrip>
        <Section title="Recent Kills" actions={<Button variant="ghost">Refresh</Button>}>
          <Table stagger>
            <TableRow><span>Loki — J121406</span><Badge tone="ok">+412M</Badge></TableRow>
            <TableRow><span>Drake — Jita</span><Badge tone="danger">−86M</Badge></TableRow>
            <TableRow><span>Ishtar — Tama</span><Badge tone="ok">+204M</Badge></TableRow>
          </Table>
        </Section>
        <Grid cols={2}>
          <Panel title="Fleet Status" glass brackets>
            <KeyValueRow label="Thunderborn HQ" value="ONLINE" tone="ok" />
            <KeyValueRow label="Fuel" value="42 days" tone="warn" />
            <KeyValueRow label="Reinforced" value="—" tone="muted" />
            <div className="b-pad-md"><ProgressBar value={72} tone="warn" /></div>
          </Panel>
          <Panel title="Loading States">
            <div className="b-pad-md"><Skeleton lines={3} /></div>
            <EmptyState>No contracts found</EmptyState>
          </Panel>
        </Grid>
        <Section title="Tabs & Actions">
          <TabStrip tabs={[{ label: 'Overview', active: true }, { label: 'Assets' }, { label: 'Journal' }]} onSelect={() => {}} />
          <Eyebrow>Card actions</Eyebrow>
          <Panel>
            <div className="b-pad-md">Hangar contents…</div>
            <ButtonGroup>
              <Button>View</Button>
              <Button>Appraise</Button>
              <Button danger>Trash</Button>
            </ButtonGroup>
          </Panel>
        </Section>
      </main>
      <ToastStack>
        <Toast tone="ok">Fit saved</Toast>
        <Toast tone="info">ESI sync complete</Toast>
      </ToastStack>
      <Modal open={modalOpen} title="Confirm Jump" onClose={() => setModalOpen(false)}>
        <KeyValueRow label="Destination" value="J121406" />
        <KeyValueRow label="Topology" value="C5 → C3 → LS" tone="accent" />
        <div style={{ marginTop: '1rem', display: 'flex', gap: '8px' }}>
          <Button variant="primary" onClick={() => setModalOpen(false)}>Jump</Button>
          <Button variant="ghost" onClick={() => setModalOpen(false)}>Cancel</Button>
        </div>
      </Modal>
      <Footer links={[{ label: 'GitHub', href: '#' }, { label: 'Status', href: '#' }]} brand="THUNDERBORN" />
    </>
  );
}

createRoot(document.getElementById('root')!).render(<Demo />);
```

- [ ] **Step 5: Build to verify**

Run: `cd design-system/react && npx vite build demo && ls demo/dist/index.html`
Expected: build succeeds.

- [ ] **Step 6: REVIEW GATE — human eyeball**

Run `cd design-system/react && npm run demo` and have the user review the page (ambient background flying with sov colors + kill blips, glass panels, animated menus, all components). **Pause here and ask the user for approval before committing.** Iterate on CSS if they request changes.

- [ ] **Step 7: Commit**

```bash
git add design-system/react/demo/
git commit -m "feat(design-system): demo harness rendering all components over ambient background"
```

---

### Task 11: Package README + final verification

**Goal:** Package documentation and a final full-suite verification.

**Files:**
- Create: `design-system/README.md`
- Modify: `.gitignore` (root — ensure `design-system/react/node_modules` and `dist` covered by the package .gitignore; verify)

**Acceptance Criteria:**
- [ ] README documents: architecture (CSS core shared with the site), the styling idiom (b-* classes + is-* modifiers, tokens via `var(--*)`), component inventory, the ambient module API, build/test commands
- [ ] Full suite green from scratch: install → test → typecheck → build → demo build

**Verify:** `cd design-system/react && npm test -- --run && npx tsc --noEmit && npm run build && npx vite build demo` → all green

**Steps:**

- [ ] **Step 1: Write `design-system/README.md`**

```markdown
# Vigilant Design System

Expressive-brutalist design system for Vigilant (EVE Online companion dashboard).
Dark `#080808`, JetBrains Mono, gold `#c8a951`, zero border-radius — with glass
surfaces, gold corner brackets, animated menus, and an ambient "flying through
New Eden" background.

## Layout

- `css/` — the source of truth. `tokens.css` (custom properties), `motion.css`
  (all keyframes), `components.css` (the `b-*` vocabulary). The Jinja2 site
  consumes these directly; the React package bundles them.
- `ambient/` — `vigilant-ambient.js`, dependency-free canvas module: real New
  Eden map flythrough, live ESI sov colors, kill blips. `mount(el, opts)` →
  `{ destroy() }`.
- `react/` — `@vigilant/ui`. Thin typed wrappers over the CSS classes.

## Styling idiom

Components are styled by `b-*` classes with `is-*` state/tone modifiers
(`is-active`, `is-danger`, `is-ok`, `is-warn`, `is-accent`, `is-muted`,
`is-glass`, `is-brackets`, `is-primary`, `is-ghost`). Design values come from
CSS custom properties in `tokens.css` (`var(--accent)`, `var(--glass-bg)`,
`var(--dur-menu)`, …). No utility-class framework; no inline hex colors.

## Commands (from `react/`)

- `npm test` — vitest
- `npm run typecheck` — tsc --noEmit
- `npm run build` — esbuild bundle + declarations → `dist/`
- `npm run demo` — Vite demo harness with every component

## Consumers

1. `@vigilant/ui` → uploaded to claude.ai/design via /design-sync
2. The Vigilant Jinja2 site (sub-project 2) links `css/*.css` from `/static/`
```

- [ ] **Step 2: Full verification from clean state**

Run: `cd design-system/react && rm -rf node_modules dist demo/dist && npm install && npm test -- --run && npx tsc --noEmit && npm run build && npx vite build demo`
Expected: everything green.

- [ ] **Step 3: Commit**

```bash
git add design-system/README.md
git commit -m "docs(design-system): package README with idiom and command reference"
```

---

## After the plan: /design-sync (main session, not a subagent task)

Once all tasks are complete and pushed, run `/design-sync` from the main session. It is user-billed, interactive (project creation approval), and has its own verification workflow — do not fold it into plan execution. Expected config: shape `package`, package dir `design-system/react`. The conventions header authored during that run should draw from `design-system/README.md`'s "Styling idiom" section.

## Self-review notes

- Spec coverage: tokens (T1), motion (T1), restyled b-* (T2), new components (T3), ambient module with sov/kills/guards (T4), React scaffold+build (T5), all 5 component groups (T5–T9), fonts (T9), demo harness (T10), README (T11), design-sync handoff (post-plan). Site rollout is explicitly out of scope per spec.
- Type consistency: `Tone`/`toneClass` defined once (T7) and used by Stat/KeyValueRow/Badge; `AmbientOptions` defined in ambient d.ts (T4) and reused by AmbientBackground (T9); Button's `variant`/`danger` API matches ButtonGroup usage in demo (T10).
- Known risk called out inline: tsc pairing of the ambient `.js` import with its `.d.ts` (T9 Step 3 includes the fallback).
```
