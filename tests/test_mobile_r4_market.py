"""Mobile R4 T1: Market search and the item page at phone width.

User decisions (all the recommended option):
  D1 A  Search results are link rows: key 1 is two lines, the item name with
        its group below (muted, smaller), each ellipsising on its own; key 2
        is a muted arrow. No toggle: the row opens the item page.
  D2 A  Order book rows are m-rows: key 1 the price (accent), key 2 the
        volume; open rows show Name (the station) then Volume. Sell and Buy
        stack into one column; all 15 rows each, no clamp.
  D3 A  The item page's stat cards (Latest avg/high/low/volume, and the order
        book's Best sell/buy and Spread) become label · value lines.
  D4 A  The price-history chart on phones: price only (no volume bars and no
        second axis) and at most 3 date labels. Desktop options are untouched.
The 30D / 90D / 1Y / ALL range buttons get 12px labels and a 40px minimum
width (the global phone rule already makes them 44px tall), and the search
box doesn't autofocus on phones (desktop keeps the attribute).

Every route here redirects (or 401s) anonymous visitors, so data-click is
safe. Templates are rendered through the market route module's own
templates.env with contexts shaped like app/routes/market.py builds them.
Names are invented."""
import functools
import re

import pytest

from app.routes import market as market_mod
from tests._mobile import (assert_mrow, assert_single_value_child, cells_rows, clamps,
                           css_section, norm, phone_block, render_page, row_keys,
                           row_labelled, rule_bodies, selectors)

_section = functools.partial(css_section, release="R4")

_STATION = "Sample Station Alpha - Long Assembly Plant Of The Outer Ring"


def _decl(body, prop, value):
    return re.search(rf"(?:^|;|\s){re.escape(prop)}\s*:\s*{re.escape(value)}\s*(?:;|$)", body)


def _phone(task="T1"):
    body, _after = phone_block(_section(task))
    return body


# ── D1 A: search results ──────────────────────────────────────────────

_RESULTS = [
    {"type_id": 2001, "type_name": "Sample Drone II", "group": "Sample Combat Drone"},
    {"type_id": 2002, "type_name": "Sample Drone I", "group": "Sample Combat Drone"},
    {"type_id": 2003, "type_name": "Sample Drone II Blueprint",
     "group": "Sample Combat Drone Blueprint"},
    {"type_id": 2004, "type_name": "Sample Navy Issue Extended Heavy Drone Of Great Length",
     "group": "Sample Frigate Hull Upgrades And Assorted Long Group"},
]


def _render_search(q="Sample", results=_RESULTS):
    return render_page(market_mod, "partials/market_search_results.html", "/market/search",
                       q=q, results=results)


def test_search_results_are_link_rows():
    html = _render_search()
    rows = assert_mrow(html, 4)
    assert len(rows) == 4
    for r, res in zip(rows, _RESULTS):
        assert r["tag"] == "a"
        assert r["attrs"]["href"] == f"/market/type/{res['type_id']}"
        cls = r["attrs"]["class"].split()
        assert {"mk-row", "m-row", "m-row--link"} <= set(cls)
        assert "data-click" not in r["attrs"]
    assert "toggleMRow" not in html
    assert "data-click" not in html


def _search_rows():
    rows = cells_rows(_render_search())
    assert len(rows) == len(_RESULTS)
    return rows


def test_search_key_one_holds_the_name_then_the_group():
    for row, res in zip(_search_rows(), _RESULTS):
        key1, key2 = row_keys(row)
        assert "m-only" in key1["attrs"]["class"].split()
        kids = [k["class"].split() for k in key1["kids"]]
        assert kids == [["mk-row-name"], ["mk-row-group"]]
        assert key1["text"] == f"{res['type_name']}{res['group']}"
        assert key2["text"] == "→"
        assert key2["attrs"].get("aria-hidden") == "true"
        assert "m-only" in key2["attrs"]["class"].split()
        assert not row_labelled(row)


def test_search_rows_keep_the_desktop_spans():
    """Desktop (D21): the name and group spans stay as the row's first two
    children, untagged, so the desktop flex line is unchanged; the phone key
    cells are m-only and never take part in it."""
    for row, res in zip(_search_rows(), _RESULTS):
        name, group = row["cells"][:2]
        assert name["attrs"] == {"class": "mk-row-name"} and name["text"] == res["type_name"]
        assert group["attrs"] == {"class": "mk-row-group"} and group["text"] == res["group"]
        for c in row["cells"][2:]:
            assert "m-only" in c["attrs"].get("class", "").split()


@pytest.mark.parametrize("q, results, hint", [
    ("S", [], "Type at least two characters"),
    ("Nothing", [], "No published items match"),
])
def test_search_hints_render_no_rows(q, results, hint):
    html = _render_search(q, results)
    assert hint in html
    assert not cells_rows(html)


# ── D2 A: order book ──────────────────────────────────────────────────

def _order(i, side):
    price = 1000.0 + i if side == "sell" else 900.0 - i
    return {"price_str": f"{price:,.2f}", "volume_str": f"{(i + 1) * 1204:,}",
            "location_name": _STATION if i % 3 == 0 else f"Sample Station {side} {i}"}


_SELL = [_order(i, "sell") for i in range(15)]
_BUY = [_order(i, "buy") for i in range(15)]


def _render_book(sell=_SELL, buy=_BUY):
    return render_page(market_mod, "partials/market_order_book.html",
                       "/market/type/2001/orders", type_id=2001,
                       sell_orders=sell, buy_orders=buy,
                       best_sell_str="1,000.00", best_buy_str="900.00",
                       spread_str="100.00", spread_pct_str="10.0%")


def test_order_rows_follow_the_contract():
    html = _render_book()
    rows = assert_mrow(html, 30)
    assert len(rows) == 30
    for r in rows:
        assert r["attrs"]["data-click"] == "toggleMRow"
        assert {"b-table-row", "m-row"} <= set(r["attrs"]["class"].split())
        assert "m-row--link" not in r["attrs"]["class"]


def _book_rows():
    rows = cells_rows(_render_book())
    assert len(rows) == len(_SELL + _BUY)
    return rows


def test_order_rows_put_the_price_first():
    for row, o in zip(_book_rows(), _SELL + _BUY):
        key1, key2 = row_keys(row)
        assert key1["text"] == o["price_str"]
        assert "color:var(--accent)" in key1["attrs"]["style"].replace(" ", "")
        assert key2["text"] == o["volume_str"]
        labelled = row_labelled(row)
        assert list(labelled) == ["Name", "Volume"]
        assert labelled["Name"]["text"] == o["location_name"]
        assert labelled["Volume"]["text"] == o["volume_str"]
        for c in labelled.values():
            assert "m-only" in c["attrs"]["class"].split()
        assert_single_value_child(row)


def test_order_rows_keep_the_desktop_location_cell():
    """Desktop (D21): price, volume, location stay the row's three flex
    cells in that order; the location keeps its hover title and is untagged,
    so phones hide it and show the m-only Name line instead."""
    for row, o in zip(_book_rows(), _SELL + _BUY):
        price, volume, loc = row["cells"][:3]
        assert "flex:1" in price["attrs"]["style"] and "flex:1" in volume["attrs"]["style"]
        assert "flex:2" in loc["attrs"]["style"]
        assert loc["attrs"]["title"] == o["location_name"] and loc["text"] == o["location_name"]
        assert "data-m" not in loc["attrs"] and "data-m-label" not in loc["attrs"]
        assert "class" not in loc["attrs"]


def test_order_book_shows_every_row_without_a_clamp():
    c = clamps(_render_book())
    assert not c.clamps and not c.showall and not c.wraps


def test_order_book_section_heads_stay_visible():
    """The book has no column-header row to hide. Its two section heads
    are the only thing telling Sell from Buy on a phone, so neither is
    m-head (or tagged in any way)."""
    html = _render_book()
    assert "m-head" not in html
    heads = re.findall(r'<div class="([^"]*)"><span class="b-label">([^<]*)</span>', html)
    assert [(cls, norm(t)) for cls, t in heads] == [
        ("b-section-head", "Sell orders (lowest 15)"),
        ("b-section-head", "Buy orders (highest 15)"),
    ]


def test_order_book_grid_has_the_stack_hook():
    html = _render_book()
    grids = re.findall(r'<div class="([^"]*)" style="([^"]*)">\s*<div class="b-section">', html)
    assert grids == [("mk-book", "display:grid;grid-template-columns:1fr 1fr;gap:0.75rem;")]


def test_empty_order_book_renders_no_rows():
    html = _render_book(sell=[], buy=[])
    assert "No sell orders in The Forge." in html and "No buy orders in The Forge." in html
    assert not cells_rows(html)


# ── D3 A: stat cards ──────────────────────────────────────────────────

_DIV_TAG = re.compile(r"<div\b|</div>")


def _div_inner(html, start):
    """The inner HTML of the <div ...> whose opening tag ends at `start`,
    up to its own matching </div>."""
    depth = 1
    for t in _DIV_TAG.finditer(html, start):
        depth += 1 if t.group() == "<div" else -1
        if not depth:
            return html[start:t.start()]
    raise AssertionError("unclosed <div>")


def _stat_strips(html):
    """Each .mk-stats strip as [(label, value, value id)]."""
    out = []
    for m in re.finditer(r'<div class="mk-stats"[^>]*>', html):
        strip = _div_inner(html, m.end())
        cards = re.findall(
            r'<div class="mk-stat"><div class="mk-stat-val"(?: id="([^"]*)")?>([^<]*)</div>'
            r'<div class="mk-stat-label">([^<]*)</div></div>', strip)
        assert len(cards) == strip.count('class="mk-stat"'), "every card is value then label"
        out.append([(label, val, vid) for vid, val, label in cards])
    return out


def _render_type(**over):
    ctx = {"type_id": 2001, "type_name": "Sample Drone II", "group_name": "Sample Combat Drone",
           "region_id": 10000002, "not_found": False}
    ctx.update(over)
    return render_page(market_mod, "market_type.html", "/market/type/2001", **ctx)


def test_item_stat_cards_have_the_line_hooks():
    strips = _stat_strips(_render_type())
    assert strips == [[
        ("Latest avg", "—", "mk-latest-avg"), ("Latest high", "—", "mk-latest-high"),
        ("Latest low", "—", "mk-latest-low"), ("Latest volume", "—", "mk-latest-vol"),
    ]]


def test_order_book_stat_cards_have_the_line_hooks():
    strips = _stat_strips(_render_book())
    assert strips == [[
        ("Best sell", "1,000.00", ""), ("Best buy", "900.00", ""),
        ("Spread (ISK)", "100.00", ""), ("Spread %", "10.0%", ""),
    ]]


# ── D4 A: price-history chart ─────────────────────────────────────────

def _chart_script(html):
    start = html.index("var TYPE_ID = 2001;")
    return html[start:html.index("</script>", start)]


def _between(text, start, stop):
    """The text from the token `start` up to the next `stop` after it."""
    i = text.index(start)
    return text[i:text.index(stop, i + len(start))]


def _phone_branch(script):
    return _between(script, "if (phoneMq && phoneMq.matches) {", "if (chart)")


def _build(script):
    return _between(script, "function build(data) {", "function drawChart(data) {")


def _draw(script):
    return _between(script, "function drawChart(data) {", "function load() {")


def _ws(text):
    """Whitespace collapsed to single spaces, so a check reads the tokens
    and not the indentation."""
    return " ".join(text.split())


# BASE's (10474ed) desktop chart config, token for token (compared through
# _ws, so re-indenting it is not a change).
_DESKTOP_CFG = """        var cfg = {
            type: 'bar',
            data: {
                labels: data.dates,
                datasets: [
                    { type: 'line', label: 'Average', data: data.average,
                      borderColor: '#c8a951', backgroundColor: 'transparent',
                      pointRadius: 0, borderWidth: 1.6, yAxisID: 'y', order: 0 },
                    { type: 'line', label: 'High', data: data.highest,
                      borderColor: 'rgba(120,170,120,0.5)', backgroundColor: 'rgba(120,170,120,0.10)',
                      pointRadius: 0, borderWidth: 0.8, yAxisID: 'y', fill: '+1', order: 1 },
                    { type: 'line', label: 'Low', data: data.lowest,
                      borderColor: 'rgba(170,120,120,0.5)', backgroundColor: 'transparent',
                      pointRadius: 0, borderWidth: 0.8, yAxisID: 'y', order: 2 },
                    { type: 'bar', label: 'Volume', data: data.volume,
                      backgroundColor: 'rgba(136,153,170,0.35)', yAxisID: 'y1', order: 3 },
                ]
            },
            options: {
                responsive: true, maintainAspectRatio: false, animation: false,
                interaction: { mode: 'index', intersect: false },
                scales: {
                    x: { ticks: { maxTicksLimit: 12, color: '#888' }, grid: { color: '#1a1a1a' } },
                    y: { position: 'left', ticks: { color: '#c8a951',
                          callback: function (v) { return fmtIsk(v); } }, grid: { color: '#1a1a1a' } },
                    y1: { position: 'right', beginAtZero: true,
                          ticks: { color: '#8899aa', callback: function (v) { return fmtIsk(v); } },
                          grid: { drawOnChartArea: false } }
                },
                plugins: {
                    legend: { labels: { color: '#ccc', boxWidth: 12 } },
                    tooltip: { callbacks: { label: function (ctx) {
                        if (ctx.dataset.label === 'Volume') return 'Volume: ' + fmtNum(ctx.parsed.y);
                        return ctx.dataset.label + ': ' + fmtIsk(ctx.parsed.y) + ' ISK';
                    } } }
                }
            }
        };
"""


def test_chart_keeps_the_desktop_options():
    script = _chart_script(_render_type())
    assert _ws(script).count(_ws(_DESKTOP_CFG)) == 1
    assert "x: { ticks: { maxTicksLimit: 12, color: '#888' }, grid: { color: '#1a1a1a' } }," in script
    assert re.search(r"\{ type: 'bar', label: 'Volume', data: data\.volume,\s+"
                     r"backgroundColor: 'rgba\(136,153,170,0\.35\)', yAxisID: 'y1', order: 3 \},", script)
    assert re.search(r"y1: \{ position: 'right', beginAtZero: true,", script)


def test_chart_phone_branch_drops_volume_and_thins_dates():
    script = _chart_script(_render_type())
    assert re.search(r"var phoneMq = window\.matchMedia \? window\.matchMedia\('\(max-width: 640px\)'\) : null;",
                     script)
    body = _phone_branch(script)
    assert "cfg.data.datasets = cfg.data.datasets.filter(function (d) { return d.yAxisID !== 'y1'; });" in body
    assert "delete cfg.options.scales.y1;" in body
    assert "cfg.options.scales.x.ticks.maxTicksLimit = 3;" in body
    # The branch runs after the desktop config is built and before the chart.
    assert script.index("var cfg = {") < script.index("if (phoneMq && phoneMq.matches)") \
        < script.index("chart = new Chart(")


def test_chart_phone_price_axis_fits_the_data():
    """D4 A drew a zoomed price axis. The chart's top-level type is 'bar',
    whose defaults start y at zero, which squashes the band into the top of
    a phone's plot; on phones y fits the data, with a little grace."""
    script = _chart_script(_render_type())
    body = _phone_branch(script)
    assert "cfg.options.scales.y.beginAtZero = false;" in body
    assert "cfg.options.scales.y.grace = '5%';" in body
    # Desktop's y axis is exactly as it was: no beginAtZero or grace of its own.
    desk_y = _between(script, "y: { position: 'left',", "y1:")
    assert "beginAtZero" not in desk_y and "grace" not in desk_y


def test_chart_drawing_is_split_from_the_stats_and_error_handling():
    """A breakpoint redraw must only redraw: build() (stat cards, hiding
    the error) runs for fresh data, and drawChart() holds everything
    Chart.js."""
    script = _chart_script(_render_type())
    build, draw = _build(script), _draw(script)
    assert "errEl.style.display = 'none';" in build
    for stat in ("mk-latest-avg", "mk-latest-high", "mk-latest-low", "mk-latest-vol"):
        assert stat in build and stat not in draw
    assert "drawChart(data);" in build
    assert "new Chart(" not in build and "var cfg" not in build and "lastData" not in build
    assert "errEl" not in draw
    draw = _ws(draw)
    assert draw.count(_ws(_DESKTOP_CFG)) == 1
    assert "lastData = data;" in draw
    assert draw.index(_ws(_DESKTOP_CFG)) < draw.index("if (phoneMq && phoneMq.matches) {") \
        < draw.index("if (chart) { chart.destroy(); }") < draw.index("chart = new Chart(")


def test_a_failed_range_keeps_the_chart_on_screen_for_the_breakpoint():
    """lastData always holds the data of the chart on screen: only
    drawChart() sets it. A failed fetch shows the error and leaves it, so
    a breakpoint crossing still redraws that chart in the new width's
    form, as BASE shows it, while the error stays visible."""
    script = _chart_script(_render_type())
    catch = _between(script, ".catch(function () {", "});")
    assert "lastData" not in catch
    assert "errEl.style.display = '';" in catch
    assert script.count("lastData =") == 2
    assert "lastData = data;" in _draw(script)


def test_chart_rebuilds_when_the_breakpoint_changes():
    script = _chart_script(_render_type())
    assert re.search(r"if \(chart && lastData\) drawChart\(lastData\);", script)
    assert "build(lastData)" not in script
    # One listener, registered once, outside build() and drawChart().
    assert script.count("addEventListener('change'") == 1
    assert "addEventListener" not in _build(script)
    assert "addEventListener" not in _draw(script)
    assert script.index("addEventListener('change'") > script.index("chart = new Chart(")
    assert "phoneMq.addEventListener('change', onBreakpoint)" in script
    assert "phoneMq.addListener(onBreakpoint)" in script


def test_not_found_page_has_no_chart_script():
    html = _render_type(type_name=None, group_name=None, not_found=True)
    assert "Item not found" in html
    assert "phoneMq" not in html and "mk-chart" not in html


# ── Search box: no autofocus on phones ────────────────────────────────

def _render_market():
    return render_page(market_mod, "market.html", "/market")


def test_search_box_keeps_autofocus_in_the_markup():
    html = _render_market()
    inputs = re.findall(r"<input\b[^>]*>", html)
    box = [i for i in inputs if 'class="mk-input"' in i]
    assert len(box) == 1 and re.search(r"\sautofocus\s", box[0])


def test_search_box_debounces_its_requests():
    """htmx's modifier syntax is delay:<time>. The old delay=300ms was an
    htmx:syntax:error that dropped the delay, so every keyup that changed
    the value sent its own request instead of one after a 300ms pause."""
    html = _render_market()
    box = [i for i in re.findall(r"<input\b[^>]*>", html) if 'class="mk-input"' in i]
    assert len(box) == 1
    assert 'hx-trigger="keyup changed delay:300ms"' in box[0]


def test_search_box_autofocus_is_dropped_on_phones_only():
    html = _render_market()
    m = re.search(r'<script nonce="test-nonce">(.*?)</script>', html[html.index('class="mk-input"'):], re.S)
    assert m, "a nonce'd script follows the search box"
    script = m.group(1)
    assert "if (!(window.matchMedia && window.matchMedia('(max-width: 640px)').matches)) return;" in script
    assert "document.querySelector('.mk-input[autofocus]')" in script
    assert "q.removeAttribute('autofocus');" in script
    # The browser may have queued the focus when the input was parsed.
    assert "if (document.activeElement === q) q.blur();" in script
    assert "requestAnimationFrame(drop)" in script


# ── CSS: R4 T1 section ────────────────────────────────────────────────

def test_t1_section_has_one_phone_block_and_no_desktop_rule():
    section = _section("T1")
    assert section.count("@media") == 1
    _body, after = phone_block(section)
    assert not after.strip()


def test_t1_css_two_line_key_is_scoped_to_the_search_list():
    css = _phone()
    lines = rule_bodies(css, ".mk-results > .m-row > .mk-row-key > *")
    for prop, value in (("display", "block"), ("overflow", "hidden"),
                        ("text-overflow", "ellipsis"), ("white-space", "nowrap")):
        assert _decl(lines, prop, value), (prop, value)
    # Every rule in the section that reaches a key cell is scoped to a list.
    for m in re.finditer(r"([^{}]+)\{", css):
        for sel in selectors(m.group(1)):
            if "data-m" in sel or "mk-row-key" in sel:
                assert sel.startswith((".mk-results ", ".mk-book ")), sel
    go = rule_bodies(css, ".mk-results > .m-row > .mk-row-go")
    assert _decl(go, "color", "var(--muted)")


def test_t1_css_stacks_the_order_book():
    css = _phone()
    assert _decl(rule_bodies(css, ".mk-book"), "grid-template-columns", "minmax(0, 1fr) !important")
    keys = rule_bodies(css, '.mk-book .m-row > [data-m="key"]')
    assert _decl(keys, "font-size", "12px !important")


def test_t1_css_turns_stat_cards_into_lines():
    css = _phone()
    # The page's <style> (.mk-stats, one class) loads after site.css; two
    # classes win on specificity, so no !important is needed.
    assert not rule_bodies(css, ".mk-stats")
    strip = rule_bodies(css, ".b-main .mk-stats")
    assert _decl(strip, "grid-template-columns", "minmax(0, 1fr)")
    assert _decl(strip, "gap", "0")
    assert "!important" not in strip
    card = rule_bodies(css, ".mk-stats > .mk-stat")
    for prop, value in (("display", "flex"), ("justify-content", "space-between"),
                        ("align-items", "baseline")):
        assert _decl(card, prop, value), (prop, value)
    assert _decl(rule_bodies(css, ".mk-stats > .mk-stat + .mk-stat"), "border-top", "none")
    label = rule_bodies(css, ".mk-stat > .mk-stat-label")
    # The label leads, although each card puts its value first, at 11px over
    # the page's 9px: every label · value line's label size.
    for prop, value in (("order", "-1"), ("flex", "none"), ("margin", "0"), ("text-align", "left"),
                        ("font-size", "11px")):
        assert _decl(label, prop, value), (prop, value)
    value = rule_bodies(css, ".mk-stat > .mk-stat-val")
    for prop, val in (("min-width", "0"), ("overflow-wrap", "anywhere"), ("text-align", "right")):
        assert _decl(value, prop, val), (prop, val)


def test_t1_css_range_buttons_are_40px_wide_tap_targets():
    css = _phone()
    btn = rule_bodies(css, "#mk-ranges > .b-btn")
    for prop, value in (("display", "flex"), ("align-items", "center"),
                        ("justify-content", "center"), ("min-width", "40px"),
                        ("font-size", "12px")):
        assert _decl(btn, prop, value), (prop, value)
    # The global phone rule gives buttons a 44px min-height; never lower it.
    heights = re.findall(r"min-height\s*:\s*(\d+)px", btn)
    assert all(int(h) >= 44 for h in heights), heights
