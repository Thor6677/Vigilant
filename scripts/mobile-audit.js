/* Vigilant mobile overflow audit (mobile design §4.7).
 *
 * Usage: open a page on the dev instance in Chrome DevTools device mode
 * (phone preset), paste this whole file into the console, press Enter.
 * Use a phone preset <=400px wide; the character tags partial still has a 400px breakpoint.
 * Closed <details> content is measured in current Chrome, so a flag there
 * means it will overflow when opened.
 * Viewport-relative widths (calc(100vw - ...)) or elements anchored to the
 * page can show false alarms in the forced pass; confirm those at a real
 * 360px preset.
 *
 * It checks the page twice: at the current viewport width, and with <html>
 * forced to 360px (a narrow Android). Both widths use the same ≤640px CSS
 * rules, so the forced pass is a fair stand-in. It reports:
 *   overflow  — elements extending past the right edge, not inside a scroller
 *   leftEdge  — elements cut off by the LEFT edge of the page. Nothing can
 *               scroll there, so it is lost wherever it sits. Ignored:
 *               anything wholly off-screen (a skip link, an off-canvas
 *               panel), visually-hidden 1px/clipped text, content a
 *               horizontal scroller has scrolled past, and the shapes
 *               inside an <svg> (its own box clips them; the <svg> itself is
 *               checked). Only the outermost offender is listed.
 *   clipped   — overflow-x:hidden/clip containers whose content is wider than
 *               they are (content silently cut off)
 *   scrollers — horizontal scroll boxes (allowed, but listed for review)
 *   pageWide  — the document itself is wider than the screen
 * <canvas> is skipped in the forced pass (charts size themselves at load).
 * Resolves to 'OK' when neither pass has overflow, leftEdge, clipping or pageWide. */
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
  // Off the left edge on purpose: 1px or clipped visually-hidden text, or
  // content a scroller has scrolled past (it comes back by scrolling).
  const visuallyHidden = (r, cs) => (r.width <= 1 && r.height <= 1) ||
    /rect\(0(px)?,? 0(px)?,? 0(px)?,? 0(px)?\)/.test(cs.clip) || cs.clipPath === 'inset(50%)';
  const scrolledAway = e => {
    for (e = e.parentElement; e && e !== document.body; e = e.parentElement) if (e.scrollLeft > 0) return true;
    return false;
  };
  const pastLeft = (e, r) => r.left + scrollX < -1 && r.right + scrollX > 0;

  const pass = async (forced) => {
    const prevWidth = h.style.width;
    if (forced) h.style.width = forced + 'px';
    await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
    const vw = forced || h.clientWidth;
    const overflow = [], leftEdge = [], clipped = [], scrollers = [];
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
      if (pastLeft(e, r) && !e.ownerSVGElement && !visuallyHidden(r, cs) && !scrolledAway(e)) {
        const p = e.parentElement, pr = p.getBoundingClientRect();
        if (!(pastLeft(p, pr) && pr.width && pr.height)) leftEdge.push(path(e) + ' left=' + Math.round(r.left + scrollX));
      }
      if (e.clientWidth >= 60 && e.scrollWidth > e.clientWidth + 8) {
        const singleLineEllipsis = (cs.textOverflow === 'ellipsis' && cs.whiteSpace === 'nowrap') || (cs.whiteSpace === 'nowrap' && e.children.length === 0);
        if (/(hidden|clip)/.test(cs.overflowX) && !singleLineEllipsis) clipped.push(path(e) + ' ' + e.clientWidth + '/' + e.scrollWidth);
        if (/(auto|scroll)/.test(cs.overflowX)) scrollers.push(path(e) + ' ' + e.clientWidth + '/' + e.scrollWidth);
      }
    }
    const pageWide = (forced ? document.body.scrollWidth : h.scrollWidth) > vw + 1;
    h.style.width = prevWidth;
    return { width: vw, pageWide, overflow: uniq(overflow), leftEdge: uniq(leftEdge), clipped: uniq(clipped), scrollers: uniq(scrollers) };
  };

  const current = await pass(0);
  const narrow = await pass(360);
  const bad = p => p.pageWide || p.overflow.length || p.leftEdge.length || p.clipped.length;
  const verdict = bad(current) || bad(narrow) ? 'ISSUES' : 'OK';
  console.log('[mobile-audit]', location.pathname, verdict, { current, narrow });
  return verdict === 'OK' ? 'OK' : { current, narrow };
})();
