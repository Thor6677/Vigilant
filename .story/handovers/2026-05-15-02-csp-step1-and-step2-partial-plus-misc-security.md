# CSP refactor — Step 1 done, Step 2 ~28% done, plus T-029/T-030 + ISS-017 filed

Continuation of the marathon from earlier today. We closed the morning's session with 28/30 tickets; the rest of the day chipped through the security phase.

## Tickets closed this segment

| Ticket | Commit | Outcome |
|---|---|---|
| **T-012** | `6d1d9de` | CSP Step 1: per-request nonce middleware + 121 `<script>`/`<style>` tags noncified across 66 templates. Closes the foundation half of the 4-step CSP roadmap; Steps 2-4 split into T-031/T-032/T-033. |
| **T-029** | `4b22f73` | Hardened `docs/nginx-sample.conf` with a copy-paste warning + Host-header contract documentation. (Map block was already present — the audit's missing-map claim was a false positive.) |
| **T-030** | `4b22f73` | Defense-in-depth `esc()` helper in `notifications.js`; wrapped every server-derived innerHTML interpolation in `_renderEvent`. Cache-busted `?v=4`. |
| **T-011** | (no code) | Closed earlier this evening after user ran sec-toolkit `/audit`. Three fix-in-progress findings (VVP-2026-001 deps, VVP-2026-002 frontend, VVP-2026-003 non-root) all flipped to fixed in audit #2. |

## In-flight: T-031 (CSP Step 2)

Commit `6e1ba5b` shipped the **delegation infrastructure** plus the **automatable slice**:

- New `static/js/actions.js` — single document-level listener for click/change/input/submit. Dispatches to `window.<name>` via `data-<event>="fnName"` attributes. Warns on missing globals during rollout.
- Loaded from `base.html` before `notifications.js` so listeners are alive when partials swap in.
- **103/364 zero-arg handlers converted** across 26 templates via a strict regex pass at `/tmp/convert_zeroarg_handlers.py`. Only the simple `<identifier>()` shape was touched — anything with args, `this`, `event`, semicolons, or Jinja interpolation stayed inline.

**262 complex handlers remain** for incremental sessions. The ticket description carries a worked example of the conversion pattern for the arg-bearing case:

```html
<!-- before -->
<button onclick="loadFitting({{ f.id }}, this)">Load</button>

<!-- after -->
<button data-click="loadFitting" data-fitting-id="{{ f.id }}">Load</button>
```

```js
window.loadFitting = function(e) {
    var id = parseInt(this.dataset.fittingId, 10);
    // ... existing logic
};
```

CSP is still in Report-Only mode through all of this, so the remaining inline handlers report-but-don't-block.

## Issue filed during the session

**ISS-017** — fitting stats panel renders raw JSON error on CSRF/auth failure. Real user-visible bug observed on the Babaroga `*BABABOOEY` fit. `recalcStats()` doesn't check `response.ok` before innerHTML'ing the response body, so a 403 JSON error gets rendered as text on the right side of the page. Workaround: refresh. Fix: defensive `response.ok` check + friendly "session expired" panel. Filed in roadmap-and-gaps, severity medium.

## Other live state

- VVP-2026-007 (CSP unsafe-inline) marked attempted twice this session — once for Step 1 (`6d1d9de`), once for Step 2 partial (`6e1ba5b`). Won't flip to `fixed` until T-031 finishes + T-032 + T-033 land.
- VVP-2026-020 (nginx sample doc) marked attempted in `4b22f73`.
- VVP-2026-021 (notifications.js innerHTML) marked attempted in `4b22f73`.
- All three are status `fix-in-progress` pending next `/audit` confirmation.

## What's left in storybloq

**Tickets:** 30/33 complete + T-031 partial. Three remain in security-hardening:
- **T-031** — 262 complex inline event handlers, conversion pattern documented in the ticket
- **T-032** — 101 inline `style=` template refactor (decision point inside: drop `unsafe-inline` from style-src or keep it)
- **T-033** — drop `unsafe-inline` + flip Report-Only → enforcing (depends on T-031 + T-032)

**Issues:** 11 open total — ISS-013 (perf rebaseline), ISS-014 (wormhole data gap, cosmetic), ISS-015 (Sacrilege override table), ISS-016 (implant persistence + clone import), ISS-017 (CSRF JSON dump) are the new ones from this session's investigations. ISS-005..ISS-010 are pre-existing UI polish.

## Tooling left in place

- `/tmp/add_csp_nonce.py` — idempotent annotator for `<script>`/`<style>` nonce attrs. Re-runnable.
- `/tmp/convert_zeroarg_handlers.py` — idempotent zero-arg handler converter. Re-running is a no-op.

## Day-total scoreboard

Started at 15/28. Closed the day at **30/33 complete** (T-029, T-030, T-031 partial, T-012, T-011 in security; the morning's 11 across Perf & Ops + Roadmap & Gaps phases). 6 issues filed across the day from investigations.
