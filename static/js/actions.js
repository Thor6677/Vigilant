/* Vigilant — CSP-safe event delegation (T-031).
 *
 * Replaces inline `onclick=` / `onchange=` / `oninput=` / `onsubmit=` /
 * `onerror=` attributes with `data-click=` / `data-change=` / `data-input=` /
 * `data-submit=` / `data-on-error=` attributes. The latter are inert HTML
 * data attributes — they don't trigger CSP's inline-script source list,
 * so they remain safe once `'unsafe-inline'` is dropped from script-src
 * in T-033.
 *
 * Conversion rules (only the simple no-arg pattern is auto-converted):
 *   <button onclick="foo()">     →  <button data-click="foo">
 *   <select onchange="bar()">    →  <select data-change="bar">
 *   <input  oninput="baz()">     →  <input  data-input="baz">
 *   <form   onsubmit="qux()">    →  <form   data-submit="qux">
 *   <input  onkeydown="k()">     →  <input  data-keydown="k">
 *   <li     onmousedown="m()">   →  <li     data-mousedown="m">
 *   <input  onfocus="f()">       →  <input  data-focus="f">
 *   <img    onerror="this.style.display='none'">
 *                                →  <img    data-on-error="hide">
 *
 * Handlers that take args, reference `this`/`event`, or contain multiple
 * statements need the `data-*` arg convention defined below (ISS-021).
 *
 * ARG-PASSING CONVENTION (ISS-021):
 *
 * Instead of inline positional args, the handler reads from `this.dataset.*`:
 *
 *   <!-- before -->
 *   <button onclick="loadFitting({{ f.id }}, this)">Load</button>
 *
 *   <!-- after -->
 *   <button data-click="loadFitting" data-fitting-id="{{ f.id }}">Load</button>
 *
 *   window.loadFitting = function(e) {
 *       // `this` is the matched element (dispatcher does fn.call(el, e))
 *       var id = parseInt(this.dataset.fittingId, 10);
 *       // ... existing logic
 *   };
 *
 * Multi-arg sites use multiple data-* attrs, NOT a JSON blob:
 *
 *   <button data-click="addModule"
 *           data-type-id="{{ r.type_id }}"
 *           data-type-name="{{ r.type_name }}"
 *           data-slot-type="{{ r.slot_type }}">Add</button>
 *
 *   window.addModule = function() {
 *       var id   = parseInt(this.dataset.typeId, 10);
 *       var name = this.dataset.typeName;
 *       var slot = this.dataset.slotType;
 *   };
 *
 * Rationale: multiple data-* attrs is idiomatic HTML5 and matches what
 * `dataset` is for. A `data-args='[1,"foo"]'` JSON-blob convention was
 * considered and rejected — Jinja quote-escaping inside JSON is fragile
 * and the per-attr approach reads more like HTML.
 *
 * For sites that wrap their call in `event.stopPropagation();`, combine
 * with `data-stop`:
 *
 *   <button data-click="viewLedger"
 *           data-char-ids="{{ c.character_id }}"
 *           data-stop>...</button>
 *
 * Special conventions added in T-031 round 3:
 *   data-on-error="hide"  — built-in shortcut for the broken-image
 *                           fallback pattern. Sets display:none on the
 *                           element when its 'error' event fires.
 *                           Listener is registered with {capture:true}
 *                           because 'error' does not bubble.
 *   data-stop             — when present on a matched element, the
 *                           dispatcher calls e.stopPropagation() before
 *                           invoking the handler. Use to migrate sites
 *                           that wrap their call in event.stopPropagation();
 *
 *                           IMPORTANT — bubble-matching semantics (ISS-024):
 *                           data-stop is read from the SAME element the
 *                           dispatcher matched via closest('[data-<event>]').
 *                           If a child has its own data-click without
 *                           data-stop, that child handler runs and the
 *                           event bubbles up — the parent's data-stop is
 *                           never inspected because closest() returns the
 *                           innermost match. To "stop above" from inside,
 *                           put data-stop on the CHILD's data-click element
 *                           (typical pattern), or wrap the children in an
 *                           outer data-click="noop" data-stop sink (the
 *                           admin_users.html pattern).
 *
 * The dispatched function must be globally reachable — either declared
 * at top-level in a template's <script> block (legacy convention used
 * throughout vigilant) or attached to `window.<name>` explicitly. The
 * dispatcher looks it up via `window[name]` at click time so functions
 * defined in htmx-loaded partials wire up automatically once the partial
 * has been swapped into the DOM.
 */
(function () {
    'use strict';

    // No-op function for stop-only sites (e.g. an element that just needs
    // to swallow a click without doing anything else). Pair with data-stop.
    window.noop = window.noop || function () {};

    // ── Shared handlers for patterns that were repeated inline ──────────
    //
    // Each of these replaces a one-liner that appeared in several templates.
    // They live here rather than being re-declared per page so there is one
    // definition to fix.

    // Was: onchange="this.form.submit()" — a select that re-submits its own
    // filter form.
    window.submitForm = window.submitForm || function () {
        if (this.form) this.form.submit();
    };

    // Was: onclick="this.select()" — click a readonly input, select its text.
    window.selectAll = window.selectAll || function () {
        if (this.select) this.select();
    };

    // Was: onclick="this.closest('.row').classList.toggle('is-expanded')" —
    // a header that expands its own card. The ancestor to toggle comes from
    // data-toggle-target; it defaults to the element itself so a missing
    // attribute degrades to a visible no-op rather than a thrown error on
    // closest(null).
    window.toggleExpanded = window.toggleExpanded || function () {
        var selector = this.dataset && this.dataset.toggleTarget;
        var target = selector ? this.closest(selector) : this;
        if (target) target.classList.toggle('is-expanded');
    };

    // Was: onclick="window.location='/somewhere'" — a whole row acting as a
    // link. The destination comes from data-href.
    window.goTo = window.goTo || function () {
        var href = this.dataset && this.dataset.href;
        if (href) window.location = href;
    };

    // Was: onclick="document.getElementById('x').style.display = ... ? '' :
    // 'none'; this.querySelector('.fit-arrow').textContent = ..." — a panel
    // header that shows/hides its body and flips a caret. The body is named
    // by data-toggle-panel (an element id); with the attribute absent it is
    // the header's next sibling, which is the other shape this took inline.
    // The caret selector defaults to .fit-arrow, the common case.
    window.togglePanel = window.togglePanel || function () {
        var id = this.dataset && this.dataset.togglePanel;
        var panel = id ? document.getElementById(id) : this.nextElementSibling;
        if (!panel) return;
        var wasHidden = panel.style.display === 'none';
        panel.style.display = wasHidden ? '' : 'none';
        var arrow = this.querySelector(
            (this.dataset && this.dataset.toggleArrow) || '.fit-arrow');
        if (arrow) arrow.textContent = wasHidden ? '\u25BE' : '\u25B8';
    };

    // Was: onerror="this.src='...'; this.onerror=null;" — swap in a fallback
    // image once, and do not loop if the fallback 404s too. Dispatched by
    // name through data-on-error, alongside the built-in "hide".
    window.imgFallback = window.imgFallback || function () {
        var src = this.dataset && this.dataset.fallbackSrc;
        // Drop the binding first: the fallback failing would re-enter here.
        this.removeAttribute('data-on-error');
        if (src) this.src = src;
    };

    // Copy a block of text to the clipboard and flash the button that asked.
    //
    // Replaces copyShoppingList / copyCompressionMultibuy / copyAppraisal,
    // three all-but-identical functions that lived in three htmx-loaded
    // fragments (shopping_list, compression_results, appraisal_results) and
    // therefore never ran at all — a fragment's script carries that
    // fragment's nonce, which the page's CSP never matches. One definition
    // here, reached by data-click, does run.
    //
    //   data-copy-from  — id of an input/textarea (or any element) whose
    //                     text to copy. The multibuy textareas use this.
    //   data-copy-text  — literal text, for a caller with nothing on the
    //                     page to point at (the appraisal total).
    //   data-copy-label — what to put back after the "Copied!" flash;
    //                     defaults to whatever the button says right now.
    window.copyToClipboard = window.copyToClipboard || function () {
        var el = this;
        var text = el.dataset.copyText;
        if (text == null) {
            var src = el.dataset.copyFrom
                ? document.getElementById(el.dataset.copyFrom) : null;
            if (!src) return;
            text = src.value != null ? src.value : src.textContent;
        }
        if (!navigator.clipboard) return;
        var label = el.dataset.copyLabel || el.textContent.trim();
        var color = el.style.color;
        var border = el.style.borderColor;
        navigator.clipboard.writeText(text).then(function () {
            el.textContent = 'Copied!';
            el.style.color = 'var(--success)';
            el.style.borderColor = 'var(--success)';
            setTimeout(function () {
                el.textContent = label;
                el.style.color = color;
                el.style.borderColor = border;
            }, 2000);
        });
    };

    // Hand a list of items to the hauling planner.
    //
    // Was sendToHauling in shopping_list and appraisal_results, plus
    // sendOresToHauling in compression_results — the same function three
    // times over the same localStorage key, each one dead for the same
    // reason. The item list came from a Jinja loop inside the script; it now
    // comes off the rows that are already in the markup, one data attribute
    // per field (the ISS-021 convention above), so the fragment needs no
    // script of its own.
    //
    //   data-haul-scope — selector for the element holding the rows;
    //                     defaults to the whole document.
    //   rows carry data-haul-name / data-haul-qty / data-haul-volume.
    window.sendToHauling = window.sendToHauling || function () {
        var scope = this.dataset.haulScope
            ? document.querySelector(this.dataset.haulScope) : document;
        if (!scope) return;
        var items = [];
        scope.querySelectorAll('[data-haul-name]').forEach(function (row) {
            items.push({
                name: row.dataset.haulName,
                qty: parseInt(row.dataset.haulQty, 10) || 0,
                volume: parseFloat(row.dataset.haulVolume) || 0,
            });
        });
        if (!items.length) return;
        try {
            localStorage.setItem('vigilant_haul_items', JSON.stringify(items));
        } catch (e) { /* private mode — the planner will just open empty */ }
        window.location.href = '/industry/hauling';
    };

    // ── Combat-profile charts ───────────────────────────────────────────
    //
    // partials/character_kill_stats.html and
    // partials/dashboard_combat_profile.html each carried a ~150-line inline
    // script drawing the same four charts. The two were identical bar the
    // canvas ids and some line wrapping, and neither ran: both fragments
    // arrive by htmx swap, and a script in swapped content is re-created as
    // inline script under the fragment's nonce, which the page's CSP header
    // never matches. Every one of those four charts was a blank canvas.
    //
    // One renderer here, driven by the markup. Each canvas says what it is
    // (data-chart-kind) and carries its own series (data-chart, JSON). The
    // parent page calls this after its swap, scoped to its own target, so
    // nothing has to know either fragment's canvas ids.
    //
    // Chart.js must be loaded by the PAGE — for the same nonce reason, a
    // library tag inside a fragment is refused too. Both parents already
    // load it.
    function _combatChart(canvas, kind, d) {
        var palette = ['#c8a951','#5eb1ff','#4ade80','#ee5555','#a855f7',
                       '#fb923c','#22d3ee','#facc15','#f472b6','#94a3b8'];
        if (kind === 'radar') {
            return new Chart(canvas, {
                type: 'radar',
                data: {
                    labels: d.labels,
                    datasets: [{
                        label: 'Profile',
                        data: d.values,
                        backgroundColor: 'rgba(200,169,81,0.18)',
                        borderColor: 'rgba(200,169,81,0.85)',
                        pointBackgroundColor: '#c8a951',
                        pointRadius: 3,
                    }]
                },
                options: {
                    responsive: true, maintainAspectRatio: false,
                    scales: {
                        r: {
                            suggestedMin: 0, suggestedMax: 100,
                            ticks: { display: false },
                            grid: { color: 'rgba(255,255,255,0.08)' },
                            angleLines: { color: 'rgba(255,255,255,0.10)' },
                            pointLabels: { color: '#bfbfbf', font: { size: 10 } }
                        }
                    },
                    plugins: {
                        legend: { display: false },
                        tooltip: { callbacks: { label: function (ctx) {
                            var suffix = [' kills', '%', ' solo', ' gang', ' ISK', ' systems'];
                            var raw = d.raw[ctx.dataIndex];
                            var v = raw;
                            if (ctx.dataIndex === 4) v = (raw / 1000000).toFixed(1) + 'M';
                            else if (typeof raw === 'number') v = (raw % 1 === 0 ? raw : raw.toFixed(1));
                            return d.labels[ctx.dataIndex] + ': ' + v + suffix[ctx.dataIndex];
                        } } }
                    }
                }
            });
        }
        if (kind === 'autopsy') {
            var labels = ['Solo PvP', 'Small Gang', 'Fleet', 'Smartbomb', 'NPC'];
            var data = [d.solo, d.small_gang, d.fleet, d.smartbomb, d.npc];
            return new Chart(canvas, {
                type: 'doughnut',
                data: { labels: labels, datasets: [{ data: data,
                    backgroundColor: ['#5eb1ff', '#4ade80', '#ee5555', '#a855f7', '#fb923c'],
                    borderColor: '#1a1a1a', borderWidth: 2 }] },
                options: {
                    responsive: true, maintainAspectRatio: false, cutout: '50%',
                    plugins: {
                        legend: { position: 'right', labels: { color: '#bfbfbf', font: { size: 10 }, boxWidth: 10 } },
                        tooltip: { callbacks: { label: function (ctx) {
                            var total = data.reduce(function (a, b) { return a + b; }, 0);
                            var pct = total ? (ctx.raw / total * 100).toFixed(0) : 0;
                            return ctx.label + ': ' + ctx.raw + ' (' + pct + '%)';
                        } } }
                    }
                }
            });
        }
        if (kind === 'profit') {
            var rows = d.rows || [];
            var names = d.names || {};
            return new Chart(canvas, {
                type: 'bar',
                data: {
                    labels: rows.map(function (r) {
                        return names[r.ship_type_id] || ('Type ' + r.ship_type_id);
                    }),
                    datasets: [
                        { label: 'Destroyed',
                          data: rows.map(function (r) { return r.isk_destroyed / 1000000; }),
                          backgroundColor: 'rgba(46,160,67,0.7)' },
                        { label: 'Lost',
                          data: rows.map(function (r) { return -r.isk_lost / 1000000; }),
                          backgroundColor: 'rgba(204,51,51,0.7)' }
                    ]
                },
                options: {
                    indexAxis: 'y', responsive: true, maintainAspectRatio: false,
                    scales: {
                        x: { stacked: true, ticks: { color: '#888', font: { size: 10 }, callback: function (v) { return v + 'M'; } }, grid: { color: 'rgba(255,255,255,0.05)' } },
                        y: { stacked: true, ticks: { color: '#bfbfbf', font: { size: 10 } }, grid: { display: false } }
                    },
                    plugins: {
                        legend: { labels: { color: '#bfbfbf', font: { size: 10 }, boxWidth: 10 } },
                        tooltip: { callbacks: { label: function (ctx) {
                            return ctx.dataset.label + ': ' + Math.abs(ctx.raw).toFixed(1) + 'M ISK';
                        } } }
                    }
                }
            });
        }
        if (kind === 'stream') {
            var datasets = d.datasets || [];
            datasets.forEach(function (ds, i) {
                ds.backgroundColor = palette[i % palette.length] + 'cc';
                ds.borderColor = palette[i % palette.length];
                ds.pointRadius = 0;
            });
            var weeks = d.weeks || 0;
            var wLabels = [];
            for (var i = 0; i < weeks; i++) wLabels.push('W' + (i - weeks + 1));
            return new Chart(canvas, {
                type: 'line',
                data: { labels: wLabels, datasets: datasets },
                options: {
                    responsive: true, maintainAspectRatio: false,
                    interaction: { mode: 'index', intersect: false },
                    scales: {
                        x: { ticks: { color: '#888', font: { size: 9 }, maxTicksLimit: 10 }, grid: { display: false } },
                        y: { stacked: true, ticks: { color: '#888', font: { size: 9 } }, grid: { color: 'rgba(255,255,255,0.05)' } }
                    },
                    plugins: { legend: { labels: { color: '#bfbfbf', font: { size: 9 }, boxWidth: 8 }, position: 'right' } }
                }
            });
        }
        return null;
    }

    window.renderCombatCharts = window.renderCombatCharts || function (root) {
        if (typeof Chart === 'undefined') return;
        var scope = root || document;
        scope.querySelectorAll('canvas[data-chart-kind]').forEach(function (canvas) {
            var payload;
            try { payload = JSON.parse(canvas.dataset.chart || 'null'); }
            catch (e) { return; }
            if (!payload) return;
            // A re-swap hands us a fresh canvas, but Chart.js keeps the old
            // instance registered against whatever was there; destroy it or
            // the library throws "Canvas is already in use".
            var existing = Chart.getChart ? Chart.getChart(canvas) : null;
            if (existing) existing.destroy();
            _combatChart(canvas, canvas.dataset.chartKind, payload);
        });
    };

    // Modal-backdrop close helper. Use on the outer modal element:
    //   <div data-click="closeModalOnBackdrop" data-modal-closer="hideMyModal">
    // Reads the closer function name from data-modal-closer and invokes it
    // only when the click landed on the backdrop itself (e.target === this).
    // Clicks inside the modal content bubble up but e.target points to the
    // inner child, so the guard returns false and the modal stays open.
    window.closeModalOnBackdrop = window.closeModalOnBackdrop || function (e) {
        if (e.target !== this) return;
        var name = this.dataset && this.dataset.modalCloser;
        if (!name) return;
        var fn = window[name];
        if (typeof fn === 'function') fn();
    };

    // Bubbling events — single document-level listener catches via bubble phase.
    var BUBBLE_EVENTS = ['click', 'change', 'input', 'submit', 'keydown',
                         'mousedown'];

    // 'focus' does not bubble, so it needs the capture phase (same treatment
    // as 'error' below). Kept separate from BUBBLE_EVENTS rather than using
    // focusin, so the attribute name still matches the event name.
    var CAPTURE_EVENTS = ['focus'];

    function dispatch(eventType, e) {
        // Walk up from e.target to find the nearest element carrying our
        // attribute for this event type. Bubbling lets a delegated listener
        // catch clicks on children (e.g. an <svg> inside a <button>).
        var attr = 'data-' + eventType;
        var el = e.target.closest('[' + attr + ']');
        if (!el) return;
        if (el.hasAttribute('data-stop')) {
            e.stopPropagation();
        }
        var name = el.getAttribute(attr);
        if (!name) return;
        var fn = window[name];
        if (typeof fn !== 'function') {
            // Surface broken bindings during the rollout. After T-031 wraps
            // we can downgrade this to a no-op.
            if (window.console && console.warn) {
                console.warn('vigilant.actions: no global function named "' + name + '" for ' + eventType);
            }
            return;
        }
        fn.call(el, e);
    }

    BUBBLE_EVENTS.forEach(function (evt) {
        document.addEventListener(evt, dispatch.bind(null, evt));
    });
    CAPTURE_EVENTS.forEach(function (evt) {
        document.addEventListener(evt, dispatch.bind(null, evt), true);
    });

    // data-confirm: a separate, simpler dispatch for the "confirm before
    // submit" pattern (replaces onsubmit="return confirm('Delete?')"). Fires
    // on the bubble path before the form's default submission. If the user
    // declines, e.preventDefault() blocks the submit. Used by many templates
    // for destructive actions. ISS-022.
    document.addEventListener('submit', function (e) {
        var el = e.target.closest && e.target.closest('[data-confirm]');
        if (!el) return;
        var msg = el.getAttribute('data-confirm');
        if (msg && !window.confirm(msg)) {
            e.preventDefault();
        }
    });

    // 'error' does not bubble — must use capture phase to catch it at the
    // document level. data-on-error="hide" is the only recognized value
    // (built-in shortcut for the broken-image fallback pattern). To dispatch
    // to a named handler, use data-on-error="myHandler" instead.
    document.addEventListener('error', function (e) {
        var el = e.target;
        // Guard against non-Element targets: 'error' also fires on window,
        // XMLHttpRequest, etc. Those lack getAttribute. The guard is
        // intentional — do not remove.
        if (!el || !el.getAttribute) return;
        var spec = el.getAttribute('data-on-error');
        if (!spec) return;
        if (spec === 'hide') {
            el.style.display = 'none';
            return;
        }
        var fn = window[spec];
        if (typeof fn === 'function') {
            fn.call(el, e);
        } else if (window.console && console.warn) {
            console.warn('vigilant.actions: no global function named "' + spec + '" for error');
        }
    }, true);

    /* Updater restart gap (Task 6).
     *
     * A deploy recreates this very app, so the panel's 2s poll WILL fail for
     * 30-60s in the middle of a normal, successful run. base.html's ISS-007
     * handler is opted out of via data-htmx-no-error on the panel, which stops
     * it overwriting the poll — but something still has to tell the operator
     * that the silence is expected rather than a hang.
     *
     * Toggling `hidden` on a sibling, never innerHTML on the panel: replacing
     * the panel's markup would destroy the hx-trigger and end the poll, so the
     * page would never notice the app coming back. That is the actual bug this
     * whole arrangement exists to avoid, and it is invisible on a fast machine
     * where the restart looks instantaneous.
     */
    function updaterRestartBanner(show) {
        var panel = document.getElementById('updater-panel');
        if (!panel) return;
        var note = document.getElementById('updater-restarting');
        if (note) note.hidden = !show;
    }

    document.addEventListener('htmx:sendError', function (e) {
        var el = e.detail && e.detail.elt;
        if (el && el.id === 'updater-panel') updaterRestartBanner(true);
    });
    document.addEventListener('htmx:responseError', function (e) {
        var el = e.detail && e.detail.elt;
        /* 502/503/504 are the edge proxy answering while the container is
         * down — the same restart, not a different failure. */
        if (el && el.id === 'updater-panel') updaterRestartBanner(true);
    });

    /* Alert banner dismiss handlers.
     *
     * These used to live as inline <script nonce="..."> blocks inside each
     * banner partial (app/templates/partials/*_alert_banners.html and
     * update_banner.html). The CSP nonce is minted per REQUEST
     * (app/middleware/csp_nonce.py), and an htmx-loaded fragment is a
     * separate request from the page that swaps it in — so a fragment's
     * inline <script> carries a nonce that never matches the page's CSP
     * header, and the browser silently refuses to run it. That is why every
     * banner's (x) button did nothing: window.dismiss*Alert was never even
     * defined. A static file loaded via <script src> is covered by
     * script-src 'self' regardless of nonce, so the handlers live here now,
     * where they actually execute — and the corresponding show/prune logic
     * moved into base.html's applyDismissState(), which runs under the
     * PAGE's own nonce on every htmx swap.
     *
     * Same localStorage keys and value shapes as the old fragment scripts,
     * so a dismissal a user already made keeps working:
     *   vigilant_dismissed_alerts       - structure + inventory + contract
     *       banners. All three render the shared .structure-alert-banner
     *       markup, and base.html's applyDismissState() already read this
     *       ONE key for all three before this fix — so that, not the
     *       contract partial's own dead dismissContractAlert /
     *       vigilant_dismissed_contract_alerts, is the key that actually
     *       governed contract-banner visibility in production. The contract
     *       partial's button now dispatches here too instead of to a
     *       same-named handler nothing ever read.
     *   vigilant_dismissed_timer_alerts - structure timer banners.
     *   vigilant_dismissed_update       - the "a newer release is
     *       available" banner, keyed by release TAG (not a plain flag) so a
     *       newer release is never pre-dismissed by the dismissal of an
     *       older one.
     */
    function _bannerDismissed(key) {
        try { return JSON.parse(localStorage.getItem(key) || '{}'); } catch (e) { return {}; }
    }
    function _setBannerDismissed(key, value) {
        try { localStorage.setItem(key, JSON.stringify(value)); } catch (e) {}
    }

    window.dismissStructureAlert = window.dismissStructureAlert || function () {
        var id = this.dataset && this.dataset.alertKey;
        if (!id) return;
        var d = _bannerDismissed('vigilant_dismissed_alerts');
        d[id] = Date.now();
        _setBannerDismissed('vigilant_dismissed_alerts', d);
        var el = document.querySelector('[data-alert-id="' + id + '"]');
        if (el) el.style.display = 'none';
    };

    window.dismissTimerAlert = window.dismissTimerAlert || function () {
        var id = this.dataset && this.dataset.alertKey;
        if (!id) return;
        var d = _bannerDismissed('vigilant_dismissed_timer_alerts');
        d[id] = Date.now();
        _setBannerDismissed('vigilant_dismissed_timer_alerts', d);
        var el = document.querySelector('[data-alert-id="' + id + '"]');
        if (el) el.style.display = 'none';
    };

    window.dismissUpdateBanner = window.dismissUpdateBanner || function () {
        var tag = this.dataset && this.dataset.updateTag;
        if (!tag) return;
        var d = _bannerDismissed('vigilant_dismissed_update');
        d[tag] = 1;
        _setBannerDismissed('vigilant_dismissed_update', d);
        var el = document.getElementById('update-banner');
        if (el) el.style.display = 'none';
    };
})();
