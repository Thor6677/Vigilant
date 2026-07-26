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
    var BUBBLE_EVENTS = ['click', 'change', 'input', 'submit'];

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
})();
