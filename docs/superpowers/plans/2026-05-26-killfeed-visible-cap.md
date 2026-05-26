# Killfeed Visible Cap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cap live-mode visible rows on `/intel/kills` at 100; surplus stays in DOM via `display:none` and is revealed by a "Show more (N hidden)" button.

**Architecture:** Pure JS/HTML change in `app/templates/intel_kills.html`. Hook into the existing `htmx:afterSwap` listener that already runs the dedupe + animation logic; add a `trimVisibleRows()` step and an `updateShowMoreButton()` step after every swap. Cap applies to htmx-driven swaps (initial load + 15s polls) only; "Load older" uses raw `fetch()` and is untouched by the hook.

**Tech Stack:** htmx 1.9.12, vanilla JS, Jinja2 template, no new dependencies.

**Spec:** `docs/superpowers/specs/2026-05-26-killfeed-visible-cap-design.md`

---

### Task 1: Cap visible rows + Show more button

**Goal:** After every htmx swap on `#kf-feed`, hide rows past index 100 and surface a "Show more (N hidden)" button that reveals the next 100 on click.

**Files:**
- Modify: `app/templates/intel_kills.html` (markup near `#kf-load-older` ~line 196; script body inside the existing IIFE ~lines 540–640)

**Acceptance Criteria:**
- [ ] Initial load shows ≤ 100 rows; Show more button hidden.
- [ ] After live polls accumulate > 100 rows, only the newest 100 are visible; Show more button shows accurate hidden count.
- [ ] Clicking Show more reveals exactly the next 100 hidden rows (oldest hidden first); counter decreases; button hides when count reaches 0.
- [ ] Detail panel adjacent to a hidden row is also hidden (no orphan panel).
- [ ] Toggling a filter chip (calls `persist()`) clears the feed; Show more button hides; cap re-applies organically as new rows arrive.
- [ ] "Load older" appends rows beyond the cap visibly (no trim); "Back to live" resets the feed and the button.

**Verify:** Manual on production after deploy — see spec §Testing. No automated tests.

**Steps:**

- [ ] **Step 1: Add the Show more button markup**

Insert a new wrapper div BEFORE the existing `#kf-load-older` div (currently at ~line 196).

Locate the existing markup:

```html
  <div id="kf-feed"
       hx-get="/intel/kills/feed"
       hx-trigger="load, every 15s"
       hx-swap="afterbegin"></div>
  <div id="kf-load-older" style="display:none;text-align:center;padding:12px;margin-top:8px;">
    <button class="kf-chip" id="kf-load-older-btn" type="button">&darr; Load older kills (last 6h)</button>
    <button class="kf-chip" id="kf-back-to-live-btn" type="button" style="display:none;">&larr; Back to live</button>
  </div>
```

Insert the Show-more wrapper between `#kf-feed` and `#kf-load-older`:

```html
  <div id="kf-feed"
       hx-get="/intel/kills/feed"
       hx-trigger="load, every 15s"
       hx-swap="afterbegin"></div>
  <div id="kf-show-more-wrap" style="display:none;text-align:center;padding:8px;">
    <button class="kf-chip" id="kf-show-more-btn" type="button">Show more (<span id="kf-show-more-count">0</span> hidden) &darr;</button>
  </div>
  <div id="kf-load-older" style="display:none;text-align:center;padding:12px;margin-top:8px;">
    <button class="kf-chip" id="kf-load-older-btn" type="button">&darr; Load older kills (last 6h)</button>
    <button class="kf-chip" id="kf-back-to-live-btn" type="button" style="display:none;">&larr; Back to live</button>
  </div>
```

- [ ] **Step 2: Add constants + helper functions inside the IIFE**

Add near the top of the main IIFE (just after the `state` initialization, before `applyToChips`, around line 244). Choose a location adjacent to `entitySplit`/`buildQS` so the functions cluster:

```javascript
    var KF_VISIBLE_CAP = 100;
    var KF_SHOW_MORE_CHUNK = 100;

    function trimVisibleRows() {
      var rows = document.querySelectorAll('#kf-feed .kf-row');
      for (var i = 0; i < rows.length; i++) {
        if (i >= KF_VISIBLE_CAP) {
          if (rows[i].style.display !== 'none') rows[i].style.display = 'none';
        } else {
          if (rows[i].style.display === 'none') rows[i].style.display = '';
        }
      }
      // Detail panels follow their row's visibility (no orphan panels).
      document.querySelectorAll('#kf-feed .kf-detail').forEach(function(p) {
        var prev = p.previousElementSibling;
        if (prev && prev.classList.contains('kf-row')) {
          p.style.display = (prev.style.display === 'none') ? 'none' : '';
        }
      });
    }

    function updateShowMoreButton() {
      var hidden = document.querySelectorAll('#kf-feed .kf-row[style*="display: none"]').length;
      var wrap = document.getElementById('kf-show-more-wrap');
      var count = document.getElementById('kf-show-more-count');
      if (!wrap || !count) return;
      if (hidden > 0) {
        count.textContent = hidden;
        wrap.style.display = '';
      } else {
        wrap.style.display = 'none';
      }
    }
```

- [ ] **Step 3: Hook trim + button update into htmx:afterSwap**

Find the existing `document.addEventListener('htmx:afterSwap', function(e) { ... })` at ~line 599. After the seen-kids dedupe block (~line 622–627) but BEFORE the kfInitialSwapDone / kf-new animation block (~line 634), add the trim calls.

Existing structure to locate:

```javascript
      // Belt-and-suspenders dedupe (in case beforeSwap missed anything).
      var seenKids = {};
      feed.querySelectorAll('.kf-row').forEach(function(row) {
        var kid = row.dataset.kid;
        if (!kid) return;
        if (seenKids[kid]) row.remove();
        else seenKids[kid] = true;
      });

      // Remove the initial-load "Loading…" placeholder.
      var loading = document.getElementById('kf-loading');
      if (loading) loading.remove();
```

Insert AFTER the loading-placeholder removal, BEFORE the `if (kfInitialSwapDone) { ... }` block:

```javascript
      // Cap visible rows at KF_VISIBLE_CAP; surplus stays in DOM (display:none).
      trimVisibleRows();
      updateShowMoreButton();
```

- [ ] **Step 4: Wire the Show more click handler**

Add at the end of the main IIFE (just before the closing `})();` around line 707), after the existing `fetchOlderChunk` override block. The button is rendered in static HTML so `getElementById` is safe at IIFE-tail time:

```javascript
    var showMoreBtn = document.getElementById('kf-show-more-btn');
    if (showMoreBtn) {
      showMoreBtn.addEventListener('click', function() {
        var hiddenRows = document.querySelectorAll('#kf-feed .kf-row[style*="display: none"]');
        // Reveal the OLDEST hidden chunk first. DOM order is newest → oldest,
        // and hiddenRows are the trailing slice past the cap, so the first N
        // entries of hiddenRows are the boundary rows just past the cap.
        // Reveal those (closest to visible window), then trim recalculates.
        var toReveal = Array.prototype.slice.call(hiddenRows, 0, KF_SHOW_MORE_CHUNK);
        toReveal.forEach(function(row) {
          row.style.display = '';
          var next = row.nextElementSibling;
          if (next && next.classList.contains('kf-detail')) next.style.display = '';
        });
        // Bump the cap so trimVisibleRows() doesn't re-hide what we just
        // revealed on the next swap. Without this, the next afterSwap would
        // immediately re-hide rows beyond index 100.
        KF_VISIBLE_CAP += toReveal.length;
        updateShowMoreButton();
      });
    }
```

- [ ] **Step 5: Reset KF_VISIBLE_CAP when feed clears**

When `persist()` clears the feed and when `resumeLive()` resets, the visible cap should reset back to 100 — otherwise the cap permanently inflates across filter changes.

In `persist()` (~line 283), inside the `if (feed) { ... }` block, alongside `kfSinceCursor = null;`:

```javascript
        feed.innerHTML = '<p id="kf-loading" style="color:var(--muted);font-size:11px;">Loading…</p>';
        kfSinceCursor = null;
        KF_VISIBLE_CAP = 100;  // reset cap; a fresh filter starts a fresh window
        if (window.htmx) htmx.trigger(feed, 'load');
```

In `resumeLive()` (~line 421), alongside `kfSinceCursor = null;`:

```javascript
        kfSinceCursor = null;
        KF_VISIBLE_CAP = 100;  // reset cap when leaving older-mode
        if (window.htmx) htmx.trigger(f, 'load');
```

- [ ] **Step 6: Sanity-check the edit**

Run a quick grep to confirm the new symbols are present exactly once each (excluding the original constant declaration):

```bash
grep -c 'KF_VISIBLE_CAP' app/templates/intel_kills.html
grep -c 'KF_SHOW_MORE_CHUNK' app/templates/intel_kills.html
grep -c 'trimVisibleRows' app/templates/intel_kills.html
grep -c 'updateShowMoreButton' app/templates/intel_kills.html
grep -c 'kf-show-more-' app/templates/intel_kills.html
```

Expected: `KF_VISIBLE_CAP` ≥ 4 (declaration + 2 resets + handler bump), `KF_SHOW_MORE_CHUNK` ≥ 2 (declaration + handler), `trimVisibleRows` ≥ 2 (declaration + call), `updateShowMoreButton` ≥ 3 (declaration + 2 calls), `kf-show-more-` ≥ 5 (wrap, btn, count IDs + 2 selectors).

If any count is lower than expected, an insertion got missed — re-read the step and fix.

- [ ] **Step 7: Commit**

```bash
git add app/templates/intel_kills.html
git commit -m "feat(kills): cap live-mode visible rows at 100 with Show more

Live polling accumulated unbounded .kf-row nodes over multi-hour
sessions (memory: project_killfeed_visible_cap). After each
htmx:afterSwap on #kf-feed, hide rows past index KF_VISIBLE_CAP=100
via display:none. A Show more (N hidden) button below the feed
reveals the next KF_SHOW_MORE_CHUNK=100 on click, bumping the cap so
the next swap doesn't re-hide what was just revealed. Filter chip
clicks and Back-to-live reset the cap to 100. Load-older mode uses
raw fetch (no htmx swap) so it bypasses the trim — explicit user
ask, kept finite by 100-row server cap.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

- [ ] **Step 8: Deploy to VPS**

```bash
ssh ijohnson@146.190.140.112 "/opt/vigilant/scripts/deploy.sh"
```

Wait for `✓ Deploy complete. Running commit: <sha>`.

- [ ] **Step 9: Verify on production**

Open `/intel/kills` in the browser (hard-refresh — inline JS is cached in any open tab) and run through spec §Testing:

1. Initial load: ≤ 100 rows visible, no Show more button.
2. Wait through ~10 polls (~2.5 min) during EU primetime. Confirm:
   - Visible row count holds at 100.
   - Show more (N hidden) button appears, counter updates over time.
3. Click Show more once: 100 more rows revealed, counter drops by 100 (or hides if it was the last chunk).
4. Click any filter chip: feed clears, repopulates, Show more hides.
5. Click Load older: rows append below current visible set without being hidden.
6. Click Back to live: feed resets cleanly, Show more hides.

If any check fails, the bug is local to the JS — fix and re-deploy. No backend rollback needed.

---

## Self-Review Notes

Ran through the spec § by § against the plan:

- §Goal, §Non-goals: Goal covered by Task 1 acceptance criteria. Non-goals (no detached storage, no true deletion, no Load-older cap, no persistence across reloads) are honored — `display:none` only, `Load older` bypassed by hook scope, no localStorage.
- §UX 1–6: Step 1 inserts the button markup; Step 3 hooks the trim into the same afterSwap that already handles dedupe; Step 4 implements the reveal click; Steps 5 covers filter-change + Back-to-live resets. The "↑ N new" pill is untouched per spec.
- §Implementation surface (constants, trim, button, click handler): Steps 2–4. The cap-bump on reveal (KF_VISIBLE_CAP += toReveal.length) is in Step 4 — added during implementation review because without it, the next swap re-hides revealed rows. Spec §Edge cases didn't call this out but it's a real bug we'd have shipped without it.
- §Edge cases:
  - Filter change → Step 5 resets KF_VISIBLE_CAP back to 100. ✓
  - Back to live → Step 5 covers resumeLive(). ✓
  - Initial load → Step 3's hook runs on the first swap; rows ≤ 100 → no hides. ✓
  - Cursor advance behaviour is governed by trim counting cumulative rows. ✓
  - Hidden-row click-to-expand: row isn't clickable when hidden; orphan-detail logic in trimVisibleRows handles any pre-existing open panel. ✓
- §Animations: Step 2 includes no transitions on the hide. ✓
- §Testing: Step 9 enumerates the same six checks. ✓
- §Rollback: single-file `git revert`. ✓

No placeholders. Symbol names consistent (`KF_VISIBLE_CAP`, `KF_SHOW_MORE_CHUNK`, `trimVisibleRows`, `updateShowMoreButton`, `kf-show-more-wrap`/`-btn`/`-count`).
