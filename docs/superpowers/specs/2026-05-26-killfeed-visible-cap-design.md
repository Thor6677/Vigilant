# Killfeed Visible Cap — Design

**Date:** 2026-05-26
**Page:** `/intel/kills` (live kill feed)
**Trigger:** Long sessions accumulate hundreds of `.kf-row` nodes from 15s live polling. Scrolling becomes the dominant input and the live-feed purpose dissolves.

## Goal

Cap the visible row count at 100. Surplus rows stay in the DOM but `display:none`, surfaced via a "Show more" button at the bottom of the feed. The cap applies to live mode only; "Load older" mode is unchanged.

## Non-goals

- Detached / JS-array storage for hidden rows. `display:none` is sufficient — browser skips layout/paint on hidden nodes, and a 30-min session realistically tops out a few hundred rows.
- True deletion of old rows. Memory says "don't lose anything — just hide".
- Capping "Load older". That mode is an explicit user request for finite paginated chunks; conflating the cap there muddies intent. User can click "Back to live" to reset.
- Persisting the expanded state across reloads. A reload always returns to the 100-row visible window.

## UX

1. Initial load renders up to `MAX_ROWS_INITIAL=100` rows (already enforced server-side). All visible.
2. Each 15s live poll prepends new rows at the top. After the swap, any `.kf-row` past index 100 (0-indexed: rows[100..]) gets `style.display = "none"`.
3. When ≥ 1 row is hidden, the existing `#kf-load-older` button area gains a sibling button: `Show more (N hidden) ↓`. Counter updates after each prepend.
4. Clicking "Show more" reveals the next chunk of `100` hidden rows (oldest hidden first). If still hidden rows remain, the counter updates; otherwise the button hides.
5. "Back to live" (older mode → live) clears the feed entirely, so the hidden buffer resets organically.
6. The "↑ N new" pill stays as-is — it's about scroll position, not the hidden buffer.

## Implementation surface

All changes in `app/templates/intel_kills.html`. No backend changes.

### Constants

```javascript
var KF_VISIBLE_CAP = 100;       // newest N rows visible by default
var KF_SHOW_MORE_CHUNK = 100;   // reveal this many per "Show more" click
```

### Trim hook

Inside the existing `htmx:afterSwap` listener on `#kf-feed`, after the seen-kids dedupe (~line 622) and before the new-row animation block (~line 634):

```javascript
trimVisibleRows();
updateShowMoreButton();
```

### `trimVisibleRows()`

```javascript
function trimVisibleRows() {
  var rows = document.querySelectorAll('#kf-feed .kf-row');
  // Hide rows past the cap (counted from newest = first in DOM order).
  for (var i = 0; i < rows.length; i++) {
    if (i >= KF_VISIBLE_CAP) {
      if (rows[i].style.display !== 'none') rows[i].style.display = 'none';
    } else {
      if (rows[i].style.display === 'none') rows[i].style.display = '';
    }
  }
  // Detail panels (.kf-detail) attached to hidden rows also hide, so an open
  // panel doesn't "float" without its parent row. Cheap: just match siblings.
  document.querySelectorAll('#kf-feed .kf-detail').forEach(function(p) {
    var prev = p.previousElementSibling;
    if (prev && prev.classList.contains('kf-row')) {
      p.style.display = (prev.style.display === 'none') ? 'none' : '';
    }
  });
}
```

### Show-more button

A new element next to (not inside) `#kf-feed`:

```html
<div id="kf-show-more-wrap" style="display:none;text-align:center;padding:8px;">
  <button class="kf-chip" id="kf-show-more-btn" type="button">
    Show more (<span id="kf-show-more-count">0</span> hidden) ↓
  </button>
</div>
```

Placed just before `#kf-load-older` (between feed and load-older). Visible only when hidden count > 0.

```javascript
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

Click handler (set up once at init):

```javascript
document.getElementById('kf-show-more-btn').addEventListener('click', function() {
  var hiddenRows = document.querySelectorAll('#kf-feed .kf-row[style*="display: none"]');
  // Reveal the oldest visible-row + 1 .. + KF_SHOW_MORE_CHUNK
  // (querySelector order = DOM order = newest → oldest)
  var toReveal = Array.prototype.slice.call(hiddenRows, 0, KF_SHOW_MORE_CHUNK);
  toReveal.forEach(function(row) {
    row.style.display = '';
    var next = row.nextElementSibling;
    if (next && next.classList.contains('kf-detail')) next.style.display = '';
  });
  updateShowMoreButton();
});
```

### Edge cases

- **Filter change (persist)**: `feed.innerHTML = '<p id="kf-loading">…</p>'` clears all rows. The next afterSwap re-runs `trimVisibleRows()` on a fresh set; hidden count = 0 → button hides. ✓
- **Back to live (resumeLive)**: same path — innerHTML cleared, fresh swap, button auto-hides. ✓
- **Initial load**: first swap brings ≤ 100 rows; nothing to hide; button stays hidden. ✓
- **Cursor advance**: live polls return only new rows. After a poll, total rows = previous + new. If previous was already at 100, every new row pushes one into the hidden buffer. The cap counts cumulative rows in DOM, not per-poll deltas, so behavior is correct.
- **Click-to-expand on a hidden row**: not reachable from UI (row isn't visible), but the detail-panel sibling logic in `trimVisibleRows()` also hides any orphan `.kf-detail` so DOM stays clean.

### Animations

- No CSS transition on the hide. The existing `.kf-new` slide-in animation only applies to rows that aren't already bound (`:not([data-bound])`); newly hidden rows ARE bound, so they don't animate.
- "Show more" reveal is instant. A fade-in would be nice-to-have but adds CSS surface area for marginal gain — pass.

## Testing

Manual verification on production after deploy:

1. Open `/intel/kills` during a busy hour (EU primetime). Confirm initial load shows 100 rows, no "Show more" button.
2. Leave page open ~10–15 min. Confirm:
   - Visible row count stays at 100.
   - "Show more (N hidden)" button appears when hidden count > 0.
   - Counter updates after each poll.
3. Click "Show more" once. Confirm 100 more rows reveal, counter updates, button hides if no more hidden.
4. Click a filter chip. Confirm feed clears, repopulates with 100 rows, button hides.
5. Click "Load older". Confirm older-mode rows append normally (no hidden), Show more button doesn't appear from older-mode appends.
6. Click "Back to live". Confirm feed resets cleanly.

No new automated tests — pure UI behavior on existing markup, no server change to assert against.

## Rollback

Single file (`app/templates/intel_kills.html`). `git revert` if the cap interferes with anything. Low blast radius.
