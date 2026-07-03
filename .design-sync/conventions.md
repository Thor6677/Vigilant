# Vigilant UI — build conventions

**Dark-first system.** Every design renders on near-black. `styles.css` styles `body` (background `var(--bg)` = #080808, JetBrains Mono, `var(--text)` = #dedede). Never place content on white; if you build your own root container, give it `background: var(--bg)`.

**No wrapper/provider is required.** Components are styled entirely by the bundled CSS. Import components from the library; load `styles.css`.

**Layout traps (real, will bite):**
- `Modal` and `ToastStack` render via portals to `document.body` — never try to visually contain them in a panel.
- Do NOT nest your own `position: fixed` elements inside `Panel glass`, `NavBar`, or anything with `backdrop-filter` — the filter makes that element the containing block and your fixed element gets trapped inside it.
- `AmbientBackground` is a live full-viewport canvas for **login screens only**; mount it once near the root, not inside panels. It draws nothing in static contexts (it fetches data at runtime).
- `Panel` bodies have **no default padding** — wrap loose content in `className="b-pad-md"`. Rows (`KeyValueRow`, `TableRow`) and `ButtonGroup` are designed to sit flush.

**Styling idiom.** Prefer the React components. For your own glue markup, use the `b-*` class vocabulary with `is-*` modifiers and `var(--*)` tokens — never invent utility classes, never use border-radius (brutalist: everything square), never hardcode hex colors.

- Text/labels: `b-label` (bold uppercase label), `b-eyebrow` (small gold), `b-muted`, `b-muted-sm`, `b-text`, `b-empty` (centered empty-state text), color utilities `is-ok is-warn is-danger is-muted is-accent`
- Spacing/layout: `b-main` (page container), `b-pad-md`, `b-grid-2`, `b-grid-3` (1px-seam grid, flat dark cells), `b-section` + `b-section-head`
- Motion: add `vg-stagger` to a container to fade-up its children in sequence
- Key tokens: colors `--bg --surface-1/2/3 --text --muted --border --accent --accent-bright --accent-dim --accent-faint --danger --success --warn --info`; glass `--glass-bg --glass-bg-heavy --glass-border --glass-blur`; depth `--shadow-1 --shadow-2 --glow-accent --glow-accent-soft`; type `--font-mono --fs-xs/sm/base/md/lg/xl --ls-tight/wide/wider/widest`; space `--sp-1…--sp-7`; motion `--dur-fast --dur-menu --dur-slow --ease-std --ease-pop`; layers `--z-ambient --z-nav --z-dropdown --z-modal --z-toast`

**Where the truth lives.** Read the bound `styles.css` (and the `_ds_bundle.css` it imports) before styling anything custom; each component's `.d.ts` is its API contract and its `.prompt.md` shows composition.

**Idiomatic example** (verified render):

```jsx
<Panel title="Fleet Status" glass brackets>
  <KeyValueRow label="Thunderborn HQ" value="ONLINE" tone="ok" />
  <KeyValueRow label="Fuel" value="42 days" tone="warn" />
  <div className="b-pad-md"><ProgressBar value={72} tone="warn" /></div>
  <ButtonGroup>
    <Button>View</Button>
    <Button variant="primary">Resupply</Button>
  </ButtonGroup>
</Panel>
```

Content voice: EVE Online domain — ISK amounts, system names (J121406, Jita), ship types, uppercase letterspaced labels.
