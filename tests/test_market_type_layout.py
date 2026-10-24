"""ISS-130: the Market item page's order book must not widen the page on desktop.

The partial's inline grid uses `1fr 1fr`, i.e. minmax(auto, 1fr), so each column
was as wide as its longest nowrap row. The page <style> block carries one rule
that lets both columns shrink (`min-width: 0`) at every width. The rule is a
desktop rule: it must not hide inside an @media block, and it only works while
both columns are direct children of .mk-book. Names are invented."""
import re
from html.parser import HTMLParser

from app.routes import market as market_mod
from tests._mobile import render_page, rule_bodies, selectors

_SEL = ".mk-book > .b-section"


def _render_type():
    return render_page(market_mod, "market_type.html", "/market/type/2001",
                       type_id=2001, type_name="Sample Drone II",
                       group_name="Sample Combat Drone", region_id=10000002,
                       not_found=False)


def _render_book():
    return render_page(market_mod, "partials/market_order_book.html",
                       "/market/type/2001/orders", type_id=2001,
                       sell_orders=[], buy_orders=[],
                       best_sell_str="1,000.00", best_buy_str="900.00",
                       spread_str="100.00", spread_pct_str="10.0%")


def _page_css(html):
    """Every <style> block of the page, comments removed (base.html has its own
    blocks, so the rule is searched for rather than taken from the first)."""
    blocks = re.findall(r"<style\b[^>]*>(.*?)</style>", html, re.S)
    return "\n".join(re.sub(r"/\*.*?\*/", "", b, flags=re.S) for b in blocks)


def _rule_depths(css, selector):
    """Brace depth at each innermost rule whose selector list has `selector`."""
    out = []
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        if selector in selectors(m.group(1)):
            before = css[:m.start()]
            out.append(before.count("{") - before.count("}"))
    return out


def test_order_book_columns_can_shrink_on_desktop():
    css = _page_css(_render_type())
    body = rule_bodies(css, _SEL)
    assert body, "no .mk-book > .b-section rule in the rendered page"
    assert re.search(r"(?:^|;|\s)min-width\s*:\s*0(?:px)?\s*(?:;|$)", body)


def test_order_book_shrink_rule_is_not_inside_a_media_block():
    depths = _rule_depths(_page_css(_render_type()), _SEL)
    assert depths, "no .mk-book > .b-section rule in the rendered page"
    assert depths == [0] * len(depths), "the rule is nested in an at-rule"


class _Children(HTMLParser):
    """Classes of the direct children of the first .mk-book element."""

    def __init__(self):
        super().__init__()
        self.stack, self.book_depth, self.children, self.done = [], None, [], False

    def handle_starttag(self, tag, attrs):
        if tag in ("br", "img", "input", "meta", "link", "hr"):
            return
        cls = (dict(attrs).get("class") or "").split()
        if not self.done and self.book_depth is None and "mk-book" in cls:
            self.book_depth = len(self.stack)
        elif self.book_depth is not None and len(self.stack) == self.book_depth + 1:
            self.children.append(cls)
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if self.stack:
            self.stack.pop()
            if self.book_depth is not None and len(self.stack) == self.book_depth:
                self.book_depth = None
                self.done = True


def test_both_columns_are_direct_children_of_the_book_grid():
    p = _Children()
    p.feed(_render_book())
    assert p.children == [["b-section"], ["b-section"]]
