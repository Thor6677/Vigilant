"""Mobile R6 T1: Server Activity (/tools/activity) on phones.

The page has no lists, so there are no m-rows. On phones:
  * the weekly heatmap fits the screen (ISS-103, user decision D1 A): the
    grid, its hour labels, the presence strip and the timezone bands share
    `30px repeat(24, minmax(0,1fr))`, cells lose their 14px floor, every
    3rd hour keeps its label, and tapping a square copies its title into an
    m-only line under the grid;
  * the 1H…ALL and LIVE pills become one "Window ▾" dropdown (D2 B), from
    tab_dropdown, fed by the same window_options loop as the pills; its
    list floats over the page and closes on an outside tap or Escape;
  * the four stat tiles go 2×2, the side-by-side chart pairs stack, and
    charts show fewer x-axis labels, re-applied in place whenever the page
    crosses 640px;
  * the live delta buttons, the clock toggle and the heatmap pills keep the
    global 44px floor (never m-tap, which would force 40px) and widen to
    44px at 12px;
  * the history browser's year row wraps, with the date span on its own line.
Desktop (>640px) renders as before (D21).

The context is built the way the route builds it (its window list, mode and
scope labels, timezone bands). Region names are invented."""
import functools
import re
from html.parser import HTMLParser

from app.intel.activity_heatmap import (MIN_KILLS_FOR_GRID, MODE_LABELS, MODES, SCOPE_LABELS,
                                        SCOPES, TZ_BANDS, empty_grid)
from app.routes import player_stats as ps_mod
from tests._mobile import SITE_CSS, VOID, css_section, norm, phone_block, render_page, rule_bodies

_section = functools.partial(css_section, release="R6")

_WINDOW_OPTIONS = [(k, v[0]) for k, v in ps_mod._WINDOWS.items()]
_SHORT = {"1h": "1H", "1d": "24H", "36h": "36H", "7d": "7D", "30d": "30D",
          "90d": "90D", "1y": "1Y", "5y": "5Y", "all": "ALL"}
_ZONES = ("highsec", "lowsec", "nullsec", "wormhole")


# ── A small DOM for the rendered page ─────────────────────────────────

class _Node:
    def __init__(self, tag, attrs, parent):
        self.tag, self.attrs, self.parent = tag, attrs, parent
        self.children, self.text = [], ""

    @property
    def classes(self):
        return self.attrs.get("class", "").split()

    def walk(self):
        for c in self.children:
            yield c
            yield from c.walk()

    def all_text(self):
        return norm(self.text + " ".join(c.all_text() for c in self.children))


class _Dom(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("#root", {}, None)
        self.cur = self.root
        self.order = []

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, {k: (v if v is not None else "") for k, v in attrs}, self.cur)
        self.cur.children.append(node)
        self.order.append(node)
        if tag not in VOID:
            self.cur = node

    def handle_endtag(self, tag):
        n = self.cur
        while n is not self.root and n.tag != tag:
            n = n.parent
        if n is not self.root:
            self.cur = n.parent

    def handle_data(self, data):
        self.cur.text += data


def _dom(html):
    d = _Dom()
    d.feed(html)
    d.close()
    return d


def _by_id(dom, id_):
    hits = [n for n in dom.order if n.attrs.get("id") == id_]
    assert len(hits) == 1, f"expected one #{id_}, found {len(hits)}"
    return hits[0]


def _by_class(dom, cls):
    return [n for n in dom.order if cls in n.classes]


# ── Rendering ─────────────────────────────────────────────────────────

def _ctx(**over):
    kills = empty_grid()
    kills[1][18] = 41
    kills[1][19] = 1240
    pcu = empty_grid(None)
    pcu[1][19] = 31500
    series = [1, 2, 3]
    ctx = dict(
        window="30d", window_label="Last 30 days", window_options=_WINDOW_OPTIONS,
        live_mode=False,
        peak_pcu=32000, mean_pcu=21000, total_kills=54000, total_isk=1.2e12,
        source_counts={"esi": 12}, labels=["Sep 01", "Sep 02", "Sep 03"],
        isk_values=series, pcu_values=series, kills_values=series,
        daily_kills_labels=["Sep 01", "Sep 02", "Sep 03"], daily_kills_counts=series,
        breakdowns_available=True, has_breakdown_data=True,
        solo_fleet_series={"solo": series, "small": series, "medium": series, "large": series},
        npc_player_series={"player": series, "npc": series},
        zone_available=True, has_zone_data=True,
        zone_series={z: series for z in _ZONES}, zone_isk_series={z: series for z in _ZONES},
        has_heatmap_data=True,
        pcu_heatmap=pcu, heatmap_grid=kills, heatmap_kills=kills,
        heatmap_mode="kills", heatmap_scope="all", heatmap_region=None,
        heatmap_region_name=None, heatmap_max=1240, heatmap_min=0,
        heatmap_kills_total=1281, heatmap_sparse=False, heatmap_zone_available=True,
        heatmap_regions=[
            {"region_id": 10000002, "region_name": "Sample Region", "jspace": False},
            {"region_id": 11000031, "region_name": "A-R00001", "jspace": True},
        ],
        heatmap_modes=[(m, MODE_LABELS[m]) for m in MODES],
        heatmap_scopes=[(s, SCOPE_LABELS[s]) for s in SCOPES],
        heatmap_days=90, heatmap_min_kills=MIN_KILLS_FOR_GRID,
        tz_bands=TZ_BANDS, presence_strip=[None] * 24,
    )
    ctx.update(over)
    return ctx


def _render(**over):
    return render_page(ps_mod, "tools_activity.html", "/tools/activity", **_ctx(**over))


def _render_live():
    """Exactly the route's live-mode context: no heatmap variables at all."""
    return render_page(ps_mod, "tools_activity.html", "/tools/activity", **{
        "window": "live", "window_label": "Live · Tranquility",
        "window_options": _WINDOW_OPTIONS, "live_mode": True,
        "peak_pcu": 0, "mean_pcu": 0, "total_kills": 0, "total_isk": 0,
        "source_counts": {}, "daily_kills_counts": [], "daily_kills_labels": [],
        "breakdowns_available": False, "has_breakdown_data": False,
        "zone_available": False, "has_zone_data": False, "has_heatmap_data": False,
    })


def _scripts(html):
    return re.findall(r"<script nonce=\"[^\"]*\">(.*?)</script>", html, flags=re.S)


def _script_with(html, needle):
    hits = [s for s in _scripts(html) if needle in s]
    assert len(hits) == 1, f"expected one inline script containing {needle!r}, found {len(hits)}"
    return hits[0]


# ── 1. Heatmap ────────────────────────────────────────────────────────

def test_heatmap_grids_carry_the_ids_the_phone_rules_target():
    dom = _dom(_render())
    grid = _by_id(dom, "pcu-heatmap")
    assert "ta-heatmap" in grid.classes
    assert len([c for c in grid.children if "ta-heatmap-cell" in c.classes]) == 7 * 24
    assert "ta-hm-strip" in _by_id(dom, "ta-presence-strip").classes
    assert "ta-hm-bands" in _by_id(dom, "ta-hm-bands").classes
    # The every-3rd-hour rule counts children: an empty gutter cell first,
    # then hour h at child h+2, so 3n+2 keeps hours 0, 3, 6 … 21.
    labels = _by_id(dom, "ta-hm-col-labels").children
    assert len(labels) == 25 and "data-hr" not in labels[0].attrs and not labels[0].all_text()
    assert [int(n.attrs["data-hr"]) for n in labels[1:]] == list(range(24))


def test_heatmap_readout_is_a_phone_only_line_after_the_grid():
    html = _render()
    dom = _dom(html)
    readout = _by_id(dom, "ta-hm-readout")
    assert "m-only" in readout.classes
    assert readout.attrs.get("aria-live") == "polite"
    assert readout.all_text(), "the line says what a tap does before the first tap"
    # Outside the grid wrap, so the absolutely positioned bands never tint it,
    # and before the legend.
    wrap = _by_class(dom, "ta-hm-grid-wrap")[0]
    assert readout not in list(wrap.walk())
    assert (html.index('class="ta-hm-grid-wrap"') < html.index('id="ta-hm-readout"')
            < html.index('id="ta-hm-legend"'))


def test_heatmap_readout_is_not_rendered_without_a_grid():
    html = _render(heatmap_sparse=True, heatmap_kills_total=11)
    assert 'id="pcu-heatmap"' not in html
    assert 'id="ta-hm-readout"' not in html


def test_heatmap_tap_copies_the_cells_title_into_the_readout():
    script = _script_with(_render(), "getElementById('pcu-heatmap')")
    assert "getElementById('ta-hm-readout')" in script
    # One delegated listener on the grid, guarded for a page without one.
    assert re.search(r"if \(el && readout\)", script)
    assert re.search(r"el\.addEventListener\('click'", script)
    assert ".closest('.ta-heatmap-cell')" in script
    assert re.search(r"readout\.textContent = cell\.title;", script)
    assert "classList.add('is-picked')" in script


def test_heatmap_cell_titles_carry_the_text_the_readout_shows():
    assert ('title="Tue 18:00–19:00 UTC · 41 kills · no pilots-online sample"'
            in _render())


# ── 2. Window dropdown ────────────────────────────────────────────────

def _window_links(dom):
    pills = [a for a in _by_class(dom, "ta-windows")[0].children if a.tag == "a"]
    details = [n for n in dom.order if n.tag == "details" and "m-tabs" in n.classes]
    assert len(details) == 1, f"expected one m-tabs dropdown, found {len(details)}"
    menu = details[0]
    links = [n for n in menu.walk() if n.tag == "a"]
    return pills, menu, links


def test_window_dropdown_lists_every_window_link_in_pill_order():
    html = _render()
    dom = _dom(html)
    pills, menu, links = _window_links(dom)
    assert [a.attrs["href"] for a in links] == [a.attrs["href"] for a in pills]
    assert [a.all_text() for a in links] == [_SHORT[w] for w, _ in _WINDOW_OPTIONS] + ["LIVE"]
    assert len(links) == len(_WINDOW_OPTIONS) + 1
    # The query string is escaped once, never twice.
    assert "&amp;amp;" not in html
    assert links[0].attrs["href"] == "/tools/activity?window=1h&mode=kills&scope=all"


def test_window_dropdown_marks_the_current_window():
    dom = _dom(_render(window="7d"))
    _, menu, links = _window_links(dom)
    active = [a for a in links if "is-active" in a.classes]
    assert [a.all_text() for a in active] == ["7D"]
    assert active[0].attrs.get("aria-current") == "page"
    summary = [n for n in menu.children if n.tag == "summary"][0]
    spans = {c.attrs.get("class"): c.all_text() for c in summary.children}
    assert spans == {"m-tabs-label": "Window", "m-tabs-current": "7D"}


def test_window_dropdown_sits_in_the_main_section_head_and_the_pills_hide_on_phones():
    dom = _dom(_render())
    pills_row = _by_class(dom, "ta-windows")[0]
    assert "m-tabs-desktop" in pills_row.classes
    _, menu, _ = _window_links(dom)
    assert menu.parent is pills_row.parent
    assert "b-section-head" in menu.parent.classes


def test_window_dropdown_carries_the_heatmap_selection():
    dom = _dom(_render(heatmap_mode="percap", heatmap_scope="ns", heatmap_region=10000002))
    _, _, links = _window_links(dom)
    assert links[0].attrs["href"] == "/tools/activity?window=1h&mode=percap&scope=ns&region=10000002"


def test_window_dropdown_renders_in_live_mode_with_live_marked():
    html = _render_live()
    dom = _dom(html)
    _, menu, links = _window_links(dom)
    assert [a.all_text() for a in links if "is-active" in a.classes] == ["LIVE"]
    summary = [n for n in menu.children if n.tag == "summary"][0]
    assert [c.all_text() for c in summary.children] == ["Window", "LIVE"]
    assert "&amp;amp;" not in html


# ── 3–8. Tiles, chart pairs, charts, tap targets, history ─────────────

def test_chart_pairs_carry_the_stacking_hook():
    dom = _dom(_render())
    pairs = _by_class(dom, "ta-pair")
    assert len(pairs) == 2
    for p in pairs:
        assert "b-section" in p.parent.classes
        assert "grid-template-columns:1fr 1fr" in p.attrs["style"]
    assert {c.attrs["id"] for p in pairs for c in p.walk() if c.tag == "canvas"} == {
        "solo-fleet-chart-30d", "npc-player-chart-30d", "zone-kills-chart-30d", "zone-isk-chart-30d"}


def test_live_delta_buttons_keep_the_44px_floor():
    """The six delta buttons are .b-btn <button>s, so the global phone rule
    already makes them 44px tall. m-tap would force 40px !important and
    shrink them; the section CSS widens them instead."""
    dom = _dom(_render_live())
    btns = [n for n in _by_id(dom, "ta-delta-btns").children if n.tag == "button"]
    assert len(btns) == 6
    for b in btns:
        assert "ta-delta-btn" in b.classes and "b-btn" in b.classes
        assert "m-tap" not in b.classes


def test_heatmap_controls_keep_the_44px_floor():
    dom = _dom(_render())
    modes = _by_class(dom, "ta-hm-mode")
    scopes = _by_class(dom, "ta-hm-scope")
    assert len(modes) == len(MODES) and len(scopes) == len(SCOPES)
    for a in modes + scopes:
        assert a.tag == "a" and "b-btn" in a.classes
        assert "m-tap" not in a.classes
    tz = _by_id(dom, "ta-tz-toggle")
    assert tz.tag == "button" and "m-tap" not in tz.classes


def test_no_button_on_the_page_carries_m_tap():
    """.m-tap's min-height:40px !important beats the 44px floor that every
    button and .b-btn gets on phones (AGENT-RULES, the 44px floor)."""
    for html in (_render(), _render_live()):
        for n in _dom(html).order:
            if n.tag == "button" or "b-btn" in n.classes:
                assert "m-tap" not in n.classes, (n.tag, n.attrs)


# ── 5. Charts: fewer x labels on phones, following the breakpoint ─────

def _head_script(html):
    return _script_with(html, "window.taFollowWidth = function")


def test_breakpoint_helper_is_defined_once_in_the_head_before_any_chart():
    for html in (_render(), _render_live()):
        helper = _head_script(html)
        head = html[:html.index("</head>")]
        assert helper in head
        assert html.index(helper) > html.index("chart-4.4.0.umd.js")
        assert "window.matchMedia('(max-width: 640px)')" in helper
        assert "window.taPhone = function" in helper


def test_breakpoint_helper_redraws_in_place_only_when_the_side_changes():
    helper = _head_script(_render())
    # The side the chart was last laid out for; a same-side change is skipped.
    assert "var lastBuiltSide = mq.matches;" in helper
    assert "if (mq.matches === lastBuiltSide) return;" in helper
    assert "lastBuiltSide = mq.matches;" in helper
    # In place: the chart object, its data, the prior-period overlay and any
    # legend-hidden series all stay. Only the x limit changes.
    assert "chart.config.options.scales.x.ticks" in helper
    assert "chart.update('none');" in helper
    assert "destroy" not in helper and "new Chart" not in helper
    # A null desktop limit restores Chart.js's own default (the live chart).
    assert "delete ticks.maxTicksLimit;" in helper
    assert "mq.addEventListener('change', relayout)" in helper


def _follows(script, chart, phone, desk):
    return f"taFollowWidth({chart}, {phone}, {desk});" in script


def test_charts_show_fewer_x_labels_on_phones_and_keep_desktop_limits():
    html = _render()
    main = _script_with(html, "getElementById('activity-chart-")
    assert "maxTicksLimit:taPhone() ? 3 : 14" in main
    assert _follows(main, "mainChart", 3, 14)
    daily = _script_with(html, "getElementById('daily-kills-chart')")
    assert "maxTicksLimit:taPhone() ? 4 : 12" in daily
    assert _follows(daily, "dailyChart", 4, 12)
    zone = _script_with(html, "getElementById('zone-kills-chart-")
    assert "maxTicksLimit:taPhone() ? 4 : 10" in zone
    assert _follows(zone, "kChart", 4, 10) and _follows(zone, "iChart", 4, 10)
    pvp = _script_with(html, "getElementById('solo-fleet-chart-")
    assert "maxTicksLimit:taPhone() ? 4 : 10" in pvp
    assert _follows(pvp, "sfChart", 4, 10) and _follows(pvp, "npChart", 4, 10)
    # No script reads the query on its own any more.
    for s in _scripts(html):
        assert "var phone = window.matchMedia" not in s


def test_live_chart_limits_x_labels_only_on_phones():
    live = _script_with(_render_live(), "getElementById('ta-live-chart')")
    assert "if (taPhone()) xTicks.maxTicksLimit = 4;" in live
    # Desktop keeps Chart.js's own default: no limit is passed there.
    assert live.count("maxTicksLimit") == 1
    # Registered once, at the lazy first build, not on every 60s refresh.
    assert live.count("taFollowWidth(") == 1
    first_build = live[live.index("_liveChart = new Chart("):]
    assert _follows(first_build, "_liveChart", 4, "null")


# ── 2b. Window dropdown: outside tap and Escape close it ──────────────

def test_window_dropdown_closes_on_outside_tap_and_escape():
    for html in (_render(), _render_live()):
        script = _script_with(html, "querySelector('.ta-windows ~ details.m-tabs')")
        # At the end of the content block, never inside the flex section head.
        assert html.index(script) > html.index('id="history-panel"')
        assert "document.addEventListener('pointerdown'" in script
        assert "!menu.contains(e.target)" in script
        assert "e.key === 'Escape'" in script
        assert "menu.querySelector('summary').focus();" in script
        assert "addEventListener('focusout'" in script
        assert script.count("menu.open = false;") == 3


def test_history_row_carries_the_wrap_hook():
    html = render_page(ps_mod, "partials/activity_history.html", "/tools/activity/history-panel")
    dom = _dom(html)
    rows = _by_class(dom, "ta-hist-nav")
    assert len(rows) == 1
    kids = [c.attrs.get("id") for c in rows[0].children]
    assert kids == ["hist-prev", "hist-slider", "hist-next", "hist-span"]
    assert "min-width:170px" in _by_id(dom, "hist-span").attrs["style"]


# ── site.css section ──────────────────────────────────────────────────

def _phone():
    phone, desktop = phone_block(_section("T1"))
    return phone, desktop


def _decls(body):
    return {k.strip(): v.strip() for k, v in
            (d.split(":", 1) for d in body.split(";") if ":" in d)}


def test_section_has_no_desktop_rules():
    _, desktop = _phone()
    assert not desktop.strip()


def test_heatmap_tracks_shrink_to_fit_on_phones():
    phone, _ = _phone()
    for sel in ("#pcu-heatmap", "#ta-hm-col-labels", "#ta-presence-strip", "#ta-hm-bands"):
        body = rule_bodies(phone, sel)
        assert _decls(body).get("grid-template-columns") == "30px repeat(24, minmax(0, 1fr))", sel


def test_heatmap_cells_lose_their_size_floor():
    phone, _ = _phone()
    d = _decls(rule_bodies(phone, "#pcu-heatmap > .ta-heatmap-cell"))
    assert d.get("min-height") == "0" and d.get("min-width") == "0"
    # iOS fires click on a plain div reliably only when it looks clickable.
    assert d.get("cursor") == "pointer"
    assert "outline" in rule_bodies(phone, "#pcu-heatmap > .ta-heatmap-cell.is-picked")


def test_hour_labels_keep_every_third_hour():
    phone, _ = _phone()
    body = rule_bodies(phone, "#ta-hm-col-labels > div[data-hr]:not(:nth-child(3n+2))")
    assert _decls(body).get("visibility") == "hidden"


def test_stat_tiles_are_two_by_two():
    phone, _ = _phone()
    d = _decls(rule_bodies(phone, ".b-pad-md > .ta-stats"))
    assert d.get("grid-template-columns") == "repeat(2, minmax(0, 1fr)) !important"


def test_chart_pairs_stack_in_one_shrinkable_column():
    phone, _ = _phone()
    d = _decls(rule_bodies(phone, ".b-section > .ta-pair"))
    assert d.get("grid-template-columns") == "minmax(0, 1fr) !important"


def test_window_dropdown_drops_its_margin_in_the_section_head():
    phone, _ = _phone()
    assert _decls(rule_bodies(phone, ".ta-windows ~ details.m-tabs")).get("margin-bottom") == "0"


def test_window_dropdown_list_floats_over_the_page():
    """An outside tap closes the menu on pointerdown. If the open list sat in
    the flow, closing it would pull everything below up before the tap's
    click lands, and the click would hit whatever moved under the finger.
    Floating the list keeps the page still."""
    phone, _ = _phone()
    assert _decls(rule_bodies(phone, ".ta-windows ~ details.m-tabs")).get("position") == "relative"
    d = _decls(rule_bodies(phone, ".ta-windows ~ details.m-tabs > .m-tabs-list"))
    assert d.get("position") == "absolute" and d.get("top") == "100%"
    assert d.get("left") == "-1px" and d.get("right") == "-1px"
    assert d.get("background") == "var(--bg)"
    # Above the page, below the sticky nav bar.
    assert d.get("z-index") == "calc(var(--z-nav) - 1)"


def test_heatmap_pills_wrap_whole_instead_of_breaking_their_label():
    """.b-btn is flex:1 (basis 0), so every pill would squeeze onto one line
    and "PILOTS ONLINE" would break inside its button. A content basis wraps
    whole pills; each still grows to fill its line."""
    phone, _ = _phone()
    d = _decls(rule_bodies(phone, ".ta-hm-group > a.b-btn"))
    assert d.get("flex") == "1 1 auto" and d.get("white-space") == "nowrap"


def test_heatmap_pills_are_centred_44px_targets_at_12px():
    """<a class="b-btn"> is display:block, so in its 44px box the label would
    sit at the top; inline-flex centres it. The selector (0,2,1) outranks
    the page's own `.ta-hm-group .b-btn` (0,2,0), which loads later."""
    phone, _ = _phone()
    d = _decls(rule_bodies(phone, ".ta-hm-group > a.b-btn"))
    assert d.get("display") == "inline-flex"
    assert d.get("align-items") == "center" and d.get("justify-content") == "center"
    assert d.get("min-width") == "44px" and d.get("font-size") == "12px"
    for decl in ("min-height", "height"):
        assert decl not in d, "the global 44px floor sets the height"


def test_delta_and_clock_buttons_are_44px_wide_at_12px():
    """Their inline font-size (9px, 10px) needs !important. Height comes from
    the global 44px floor and is never set here."""
    phone, _ = _phone()
    for sel in ("#ta-delta-btns > .ta-delta-btn", "#ta-tz-toggle"):
        d = _decls(rule_bodies(phone, sel))
        assert d.get("min-width") == "44px", sel
        assert d.get("font-size") == "12px !important", sel
        assert "min-height" not in d and "height" not in d, sel


def test_section_never_sets_a_40px_height():
    phone, _ = _phone()
    assert not re.search(r"(min-)?height\s*:\s*40px", phone)
    assert "m-tap" not in phone


def test_region_select_takes_a_full_line():
    phone, _ = _phone()
    assert _decls(rule_bodies(phone, ".ta-hm-controls > .ta-hm-group")).get("flex") == "1 1 100%"
    sel = _decls(rule_bodies(phone, "#ta-region-form > #ta-region-select"))
    assert sel.get("flex") == "1 1 100%" and sel.get("max-width") == "none"
    flt = _decls(rule_bodies(phone, "#ta-region-form > #ta-region-filter"))
    assert flt.get("width") == "auto" and flt.get("min-width") == "0"


def test_history_row_wraps_with_the_span_on_its_own_line():
    phone, _ = _phone()
    assert _decls(rule_bodies(phone, ".ta-hist-nav")).get("flex-wrap") == "wrap"
    assert _decls(rule_bodies(phone, ".ta-hist-nav > .b-btn")).get("flex") == "none"
    span = _decls(rule_bodies(phone, ".ta-hist-nav > #hist-span"))
    assert span.get("flex") == "1 1 100%" and span.get("min-width") == "0 !important"


def test_readout_is_styled_on_phones():
    phone, _ = _phone()
    d = _decls(rule_bodies(phone, ".ta-hm-readout"))
    assert d.get("font-size") == "12px"


def test_new_hook_classes_have_no_rules_outside_the_phone_block():
    """ta-pair, ta-hist-nav, ta-hm-readout and is-picked exist only for
    phones; m-tap and m-tabs-desktop are the contract's phone-only classes.
    None of them may gain a desktop rule anywhere in site.css."""
    with open(SITE_CSS, encoding="utf-8") as fh:
        css = re.sub(r"/\*.*?\*/", "", fh.read(), flags=re.S)
    phone, _ = _phone()
    for cls in ("ta-pair", "ta-hist-nav", "ta-hm-readout", "is-picked"):
        assert css.count(cls) == phone.count(cls) > 0, cls
