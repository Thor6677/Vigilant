/* Vigilant mobile overflow audit (mobile design §4.7).
 *
 * Usage: open a page on the dev instance in Chrome DevTools device mode
 * (phone preset), paste this whole file into the console, press Enter.
 *
 * It checks the page twice: at the current viewport width, and with <html>
 * forced to 360px (a narrow Android). Both widths use the same ≤640px CSS
 * rules, so the forced pass is a fair stand-in. It reports:
 *   overflow  — elements extending past the right edge, not inside a scroller
 *   clipped   — overflow-x:hidden/clip containers whose content is wider than
 *               they are (content silently cut off)
 *   scrollers — horizontal scroll boxes (allowed, but listed for review)
 *   pageWide  — the document itself is wider than the screen
 * <canvas> is skipped in the forced pass (charts size themselves at load).
 * Resolves to 'OK' when neither pass has overflow, clipping or pageWide. */
(async () => {
  const h = document.documentElement;
  const desc = e => {
    let s = e.tagName.toLowerCase();
    if (e.id) s += '#' + e.id;
    if (e.classList.length) s += '.' + [...e.classList].slice(0, 2).join('.');
    return s;
  };
  const path = e => {
    const a = [];
    for (let i = 0; i < 3 && e && e !== document.body; i++) { a.unshift(desc(e)); e = e.parentElement; }
    return a.join(' > ');
  };
  const insideScroller = e => {
    for (e = e.parentElement; e && e !== document.body; e = e.parentElement) {
      if (/(auto|scroll|hidden|clip)/.test(getComputedStyle(e).overflowX)) return true;
    }
    return false;
  };
  const uniq = a => a.filter((v, i, s) => s.indexOf(v) === i);

  const pass = async (forced) => {
    const prevWidth = h.style.width;
    if (forced) h.style.width = forced + 'px';
    await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
    const vw = forced || h.clientWidth;
    const overflow = [], clipped = [], scrollers = [];
    for (const e of document.querySelectorAll('body *')) {
      const r = e.getBoundingClientRect();
      if (!r.width || !r.height) continue;
      const cs = getComputedStyle(e);
      if (cs.position === 'fixed') continue;
      if (forced && e.tagName === 'CANVAS') continue;
      if (r.right > vw + 1 && !insideScroller(e)) {
        const pr = e.parentElement.getBoundingClientRect();
        if (!(pr.right > vw + 1)) overflow.push(path(e) + ' right=' + Math.round(r.right));
      }
      if (e.clientWidth >= 60 && e.scrollWidth > e.clientWidth + 8) {
        const singleLineEllipsis = cs.textOverflow === 'ellipsis' || (cs.whiteSpace === 'nowrap' && e.children.length === 0);
        if (/(hidden|clip)/.test(cs.overflowX) && !singleLineEllipsis) clipped.push(path(e) + ' ' + e.clientWidth + '/' + e.scrollWidth);
        if (/(auto|scroll)/.test(cs.overflowX)) scrollers.push(path(e) + ' ' + e.clientWidth + '/' + e.scrollWidth);
      }
    }
    const pageWide = h.scrollWidth > vw + 1;
    h.style.width = prevWidth;
    return { width: vw, pageWide, overflow: uniq(overflow), clipped: uniq(clipped), scrollers: uniq(scrollers) };
  };

  const current = await pass(0);
  const narrow = await pass(360);
  const bad = p => p.pageWide || p.overflow.length || p.clipped.length;
  console.log('[mobile-audit]', location.pathname, { current, narrow });
  return bad(current) || bad(narrow) ? { current, narrow } : 'OK';
})();
