/* Vigilant — CSP-safe event delegation (T-031).
 *
 * Replaces inline `onclick=` / `onchange=` / `oninput=` attributes with
 * `data-click=` / `data-change=` / `data-input=` attributes. The latter
 * are inert HTML data attributes — they don't trigger CSP's inline-script
 * source list, so they remain safe once `'unsafe-inline'` is dropped
 * from script-src in T-033.
 *
 * Conversion rules (only the simple no-arg pattern is auto-converted):
 *   <button onclick="foo()">     →  <button data-click="foo">
 *   <select onchange="bar()">    →  <select data-change="bar">
 *   <input  oninput="baz()">     →  <input  data-input="baz">
 *
 * Handlers that take args, reference `this`/`event`, contain semicolons,
 * or interpolate `{{ }}` are NOT auto-converted by the migration script;
 * they stay inline (still report-only) and will be converted manually
 * in a future T-031 follow-up session.
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

    // Map DOM event → data-* attribute name. Add entries here when extending
    // coverage to more event types.
    var EVENTS = {
        click: 'click',
        change: 'change',
        input: 'input',
        submit: 'submit',
    };

    function dispatch(eventType, e) {
        // Walk up from e.target to find the nearest element carrying our
        // attribute for this event type. Bubbling lets a delegated listener
        // catch clicks on children (e.g. an <svg> inside a <button>).
        var attr = 'data-' + eventType;
        var el = e.target.closest('[' + attr + ']');
        if (!el) return;
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

    Object.keys(EVENTS).forEach(function (evt) {
        document.addEventListener(evt, dispatch.bind(null, evt));
    });
})();
