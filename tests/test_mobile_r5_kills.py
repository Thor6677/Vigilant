"""Mobile R5 T1: Kill Feed, Kill Search and the kill detail panel on phones.

User picks (R5): D1 A, kill rows are one line (ship thumb, "victim · ship",
ISK) and a tap still loads the full detail below the row; D2 A, the filter
rows fold behind one "Filters" toggle; D3 A, the attackers list shows the
first 10, then "Show all N".

The kill rows keep their own JS click handler (bindRowClicks), so they are
NOT m-rows: the phone layout is section CSS plus an m-only "victim · ship"
line, with the desktop meta lines tagged m-hide. That also keeps the 100-row
cap working, since it hides surplus rows with an inline display:none that an
m-row's `display: grid !important` would beat.

Desktop (>640px) renders as before (D21): every addition is m-only, every
desktop-only line is m-hide, and the section holds no desktop rule.

Names and ids are invented."""
import functools
import re
from datetime import datetime
from html.parser import HTMLParser
from types import SimpleNamespace

import pytest

from app.routes import intel_kills as kills_mod
from app.routes import intel_kills_search as search_mod
from tests._mobile import (VOID, clamps, css_section, mrows, norm, phone_block,
                           render_page, rule_bodies, selectors, source)

_section = functools.partial(css_section, release="R5")


# ── a tiny DOM for the rendered HTML ──────────────────────────────────

class _Node:
    def __init__(self, tag, attrs, parent):
        self.tag, self.attrs, self.parent = tag, attrs, parent
        self.children, self.text = [], ""

    @property
    def classes(self):
        return self.attrs.get("class", "").split()

    def all_text(self):
        return norm(self.text + " ".join(c.all_text() for c in self.children))

    def walk(self):
        for c in self.children:
            yield c
            yield from c.walk()

    def find_all(self, cls=None, tag=None, **attrs):
        out = []
        for n in self.walk():
            if cls is not None and cls not in n.classes:
                continue
            if tag is not None and n.tag != tag:
                continue
            if any(n.attrs.get(k.replace("_", "-")) != v for k, v in attrs.items()):
                continue
            out.append(n)
        return out

    def find(self, cls=None, tag=None, **attrs):
        hits = self.find_all(cls, tag, **attrs)
        assert hits, f"no element with class={cls!r} tag={tag!r} attrs={attrs!r}"
        return hits[0]


class _Tree(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("#root", {}, None)
        self.cur = self.root

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, {k: (v if v is not None else "") for k, v in attrs}, self.cur)
        self.cur.children.append(node)
        if tag not in VOID:
            self.cur = node

    def handle_endtag(self, tag):
        n = self.cur
        while n is not None and n.tag != tag:
            n = n.parent
        if n is not None and n.parent is not None:
            self.cur = n.parent

    def handle_data(self, data):
        self.cur.text += data


def _dom(html):
    p = _Tree()
    p.feed(html)
    p.close()
    return p.root


# ── fixtures: contexts shaped the way the routes build them ───────────

def _kill(i, **over):
    k = {
        "killmail_id": 120000000 + i,
        "killmail_time": f"2026-10-04T12:0{i}:00",
        "system_name": f"Sample System {i}",
        "system_band": "ls",
        "system_class_label": None,
        "victim_pilot": f"Sample Victim {i}",
        "victim_corp": f"Sample Corp {i}",
        "victim_ship": f"Sample Ship {i}",
        "victim_ship_type_id": 587,
        "top_attacker_pilot": f"Sample Hunter {i}",
        "top_attacker_corp": "Sample Hunters",
        "gang_size": i + 1,
        "isk": 1.5e9 * (i + 1),
        "is_npc": False,
    }
    k.update(over)
    return k


_KILLS = [_kill(0), _kill(1, is_npc=True), _kill(2, victim_corp="")]


def _feed(kills=_KILLS):
    return render_page(kills_mod, "partials/intel_kills_feed.html", "/intel/kills/feed",
                       kills=kills, total_in_buffer=len(kills), newest_id=kills[0]["killmail_id"])


def _search_results(kills=_KILLS):
    return render_page(search_mod, "partials/intel_kills_search_results.html",
                       "/intel/kills/search/results", kills=kills, total_count=len(kills),
                       total_isk=sum(k["isk"] for k in kills), newest_cursor="n",
                       oldest_cursor="o", live=False, defaulted_time=False,
                       defaulted_from_end=False)


def _attacker(i):
    return {"pilot_id": 91000000 + i, "pilot": f"Sample Attacker {i}", "corp": "Sample Hunters",
            "ship_id": 587, "ship": "Sample Frigate", "weapon": "—", "damage": 100 - i,
            "damage_pct": 100 - i, "share_pct": 7.1, "final_blow": i == 0,
            "internal_link": False, "has_damage": True}


def _detail(n_attackers):
    km = SimpleNamespace(victim_ship_type_id=587, killmail_time=datetime(2026, 10, 4, 12, 0),
                         victim_character_id=90000001, solar_system_id=30000001)
    slots = [{"label": "High", "items": [
        {"type_id": 2000 + j, "name": f"Sample Module {j}", "qty_destroyed": 1,
         "qty_dropped": 0, "destroyed": True, "dropped": False} for j in range(3)]}]
    return render_page(kills_mod, "partials/intel_kills_detail.html", "/intel/kills/1/detail",
                       kid=120000001, km=km, victim_pilot="Sample Victim",
                       victim_corp="Sample Corp", victim_ship="Sample Ship",
                       system_name="Sample System", slots=slots, items_present=True,
                       attackers=[_attacker(i) for i in range(n_attackers)],
                       attacker_count=n_attackers, total_damage=1000, total_destroyed=2.5e9)


def _card(i):
    return {"killmail_id": 120000100 + i, "type_id": 35832, "type_name": f"Sample Hull {i}",
            "isk_fmt": "12.3B", "victim_corp": "Sample Corp", "victim_alliance": "",
            "system_id": 30000001, "system_name": "Sample System", "system_band": "ns"}


def _top():
    return render_page(kills_mod, "partials/intel_kills_top.html", "/intel/kills/top",
                       structures=[_card(i) for i in range(6)], ships=[_card(i) for i in range(6, 12)])


def _feed_page():
    return render_page(kills_mod, "intel_kills.html", "/intel/kills")


def _search_page():
    return render_page(search_mod, "intel_kills_search.html", "/intel/kills/search")


# ── 1. kill rows: one line on phones (D1 A) ───────────────────────────

@pytest.mark.parametrize("render", [_feed, _search_results], ids=["feed", "search"])
def test_kill_rows_carry_a_phone_only_victim_ship_line(render):
    rows = _dom(render()).find_all("kf-row")
    assert len(rows) == 3
    for k, row in zip(_KILLS, rows):
        lines = row.find_all("kf-meta-line")
        assert len(lines) == 1, "one phone line per row"
        line = lines[0]
        assert "m-only" in line.classes, "the one-line copy is phone-only (desktop unchanged)"
        assert line.parent.tag == "div" and "kf-meta" in line.parent.classes
        assert line.find("kf-meta-victim").all_text() == k["victim_pilot"]
        assert line.find("kf-meta-ship").all_text() == f"· {k['victim_ship']}"
        # A named pilot's corp is not on the phone line; it stays in the
        # detail panel.
        if k["victim_corp"]:
            assert k["victim_corp"] not in line.all_text()


# Structures have no pilot: the feed's enrich emits "?" and Kill Search's
# emits "NPC". NPC-ship victims read the same. Kill Search has no row
# template of its own; it includes the feed partial, so both renders cover it.
_NO_PILOT = [
    _kill(3, victim_pilot="?", victim_corp="Sample Structure Owners", victim_ship="Astrahus"),
    _kill(4, victim_pilot="NPC", victim_corp="Sample Pirate Faction", victim_ship="Sample Frigate",
          is_npc=True),
    _kill(5, victim_pilot="?", victim_corp="", victim_ship="Astrahus"),
    _kill(6, victim_pilot="NPC", victim_corp="", victim_ship="Sample Frigate", is_npc=True),
]


@pytest.mark.parametrize("render", [_feed, _search_results], ids=["feed", "search"])
@pytest.mark.parametrize("i,victim", [
    (0, "[Sample Structure Owners]"),   # structure: the owner corp, as desktop shows it
    (1, "[Sample Pirate Faction]"),     # NPC victim with a corp
    (2, "?"),                           # no corp to show: same bare "?" as desktop
    (3, "NPC"),
])
def test_phone_line_shows_the_owner_corp_when_there_is_no_pilot(render, i, victim):
    k = _NO_PILOT[i]
    row = _dom(render(kills=_NO_PILOT)).find_all("kf-row")[i]
    assert row.attrs["data-kid"] == str(k["killmail_id"])
    line = row.find("kf-meta-line")
    assert line.find("kf-meta-victim").all_text() == victim
    assert line.find("kf-meta-ship").all_text() == f"· {k['victim_ship']}"
    # Desktop still reads pilot [corp] · ship.
    top = row.find("kf-meta-top")
    assert top.find(tag="strong").all_text() == k["victim_pilot"]
    if k["victim_corp"]:
        assert f"[{k['victim_corp']}]" in top.all_text()


@pytest.mark.parametrize("render", [_feed, _search_results], ids=["feed", "search"])
def test_phone_line_carries_the_npc_badge(render):
    kills = [_kill(0), _kill(1, is_npc=True), _NO_PILOT[0], _NO_PILOT[1]]
    for k, row in zip(kills, _dom(render(kills=kills)).find_all("kf-row")):
        line = row.find("kf-meta-line")
        top = row.find("kf-meta-top")
        want = 1 if k["is_npc"] else 0
        assert len(top.find_all("kf-npc-badge")) == want, "desktop keeps its badge"
        badges = line.find_all("kf-npc-badge")
        assert len(badges) == want
        if want:
            b = badges[0]
            assert b.tag == "span" and b.all_text() == "NPC"
            # The badge sits beside the truncating text, not inside it, so a
            # long name's ellipsis can't clip it.
            assert b.parent is line
            assert not line.find("kf-meta-text").find_all("kf-npc-badge")


@pytest.mark.parametrize("render", [_feed, _search_results], ids=["feed", "search"])
def test_phone_line_text_is_one_truncating_span(render):
    for row in _dom(render()).find_all("kf-row"):
        line = row.find("kf-meta-line")
        texts = [c for c in line.children if "kf-meta-text" in c.classes]
        assert len(texts) == 1, "victim and ship share one truncating span"
        assert texts[0].find("kf-meta-victim") and texts[0].find("kf-meta-ship")


@pytest.mark.parametrize("render", [_feed, _search_results], ids=["feed", "search"])
def test_desktop_meta_lines_are_hidden_on_phones(render):
    for row in _dom(render()).find_all("kf-row"):
        top, bot = row.find("kf-meta-top"), row.find("kf-meta-bot")
        assert "m-hide" in top.classes
        assert "m-hide" in bot.classes, "system · killed by · gang is in the detail panel"
        # Desktop content is untouched.
        assert "killed by" in bot.all_text()


@pytest.mark.parametrize("render", [_feed, _search_results], ids=["feed", "search"])
def test_kill_rows_keep_their_own_click_and_are_not_m_rows(render):
    """bindRowClicks loads the detail panel. An m-row would add a second tap
    behaviour and its display:grid !important would beat the 100-row cap's
    inline display:none."""
    html = render()
    assert mrows(html) == []
    for row in _dom(html).find_all("kf-row"):
        assert "data-click" not in row.attrs
        assert "style" not in row.attrs


def test_the_row_cap_still_hides_rows_inline():
    """Untagged rows keep the cap's inline style.display mechanism; nothing
    in the phone CSS may set display on a row or a detail panel itself."""
    src = source("intel_kills.html")
    assert "rows[i].style.display = 'none'" in src
    assert '.kf-row[style*="display: none"]' in src
    phone, _ = phone_block(_section("T1"))
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", phone):
        for sel in selectors(m.group(1)):
            subject = sel.split()[-1] if sel.split() else ""
            if re.fullmatch(r"\.kf-(row|detail)(\.[\w-]+|:[\w-]+(\([^)]*\))?)*", subject):
                assert not re.search(r"(^|;)\s*display\s*:", m.group(2)), (
                    f"{sel!r} sets display; the 100-row cap relies on inline display:none")


def test_row_css_is_one_truncating_line():
    phone, _ = phone_block(_section("T1"))
    row = rule_bodies(phone, ".kf-row")
    assert re.search(r"grid-template-columns:\s*36px minmax\(0, ?1fr\) auto", row)
    assert re.search(r"min-width:\s*0", rule_bodies(phone, ".kf-row > .kf-meta"))
    # The line is a flex row: the text truncates, the NPC badge never shrinks.
    line = rule_bodies(phone, ".kf-meta-line")
    assert re.search(r"display:\s*flex", line)
    assert re.search(r"align-items:\s*center", line)
    text = rule_bodies(phone, ".kf-meta-line > .kf-meta-text")
    for decl in (r"min-width:\s*0", r"white-space:\s*nowrap", r"overflow:\s*hidden",
                 r"text-overflow:\s*ellipsis"):
        assert re.search(decl, text), decl
    assert re.search(r"flex:\s*none", rule_bodies(phone, ".kf-meta-line > .kf-npc-badge"))
    assert re.search(r"display:\s*none", rule_bodies(phone, ".kf-row .kf-ago"))


# ── 2–3. the detail panel: one column, attackers clamped at 10 ────────

def test_detail_attackers_clamp_with_show_all_past_ten():
    html = _detail(14)
    c = clamps(html)
    assert c.wraps == 1 and c.nested_wraps == 0
    assert len(c.clamps) == 1 and c.clamps[0]["children"] == 14
    assert len(c.showall) == 1
    btn = c.showall[0]
    assert btn["in_wrap"]
    assert btn["text"] == "Show all 14"
    a = btn["attrs"]
    assert a.get("type") == "button"
    assert set(a.get("class", "").split()) >= {"m-only", "m-showall"}
    assert a.get("data-click") == "toggleExpanded"
    assert a.get("data-toggle-target") == ".m-clamp-wrap"
    right = _dom(html).find("kf-detail-right")
    assert "m-clamp-wrap" in right.classes
    # Every attacker sits in the clamp; the heading stays outside it.
    clamp = right.find("m-clamp")
    assert len(clamp.find_all("kf-attacker-compact")) == 14
    assert right.find_all(tag="h4") and not clamp.find_all(tag="h4")


@pytest.mark.parametrize("n", [0, 3, 10])
def test_detail_has_no_show_all_at_ten_or_fewer(n):
    c = clamps(_detail(n))
    assert c.showall == []
    assert len(c.clamps) == 1 and c.clamps[0]["children"] == n


def test_detail_zkillboard_link_is_a_tap_target():
    link = _dom(_detail(3)).find("kf-zkb-link")
    assert "m-tap" in link.classes


def test_detail_css_stacks_and_unclamps():
    phone, _ = phone_block(_section("T1"))
    grid = rule_bodies(phone, ".kf-detail-grid")
    assert re.search(r"grid-template-columns:\s*minmax\(0, ?1fr\)\s*;", grid)
    assert re.search(r"max-height:\s*none", rule_bodies(phone, ".kf-detail.shown"))
    right = rule_bodies(phone, ".kf-detail-right")
    assert re.search(r"max-height:\s*none", right)
    assert re.search(r"overflow:\s*visible", right)


# ── 4. filters fold behind one toggle (D2 A) ──────────────────────────

@pytest.mark.parametrize("render,box,row_cls", [
    (_feed_page, "kf-filters", "kf-filter-row"),
    (_search_page, "kfs-filters", "kfs-filter-row"),
], ids=["feed", "search"])
def test_filters_fold_behind_a_phone_only_toggle(render, box, row_cls):
    dom = _dom(render())
    filters = dom.find_all(box)
    assert len(filters) == 1
    fold = filters[0]
    assert "kf-fold" in fold.classes, "the fold hook"
    assert "is-expanded" not in fold.classes, "phones start folded"
    toggles = dom.find_all("kf-fold-toggle")
    assert len(toggles) == 1
    t = toggles[0]
    assert t.tag == "button" and t.attrs.get("type") == "button"
    assert "m-only" in t.classes, "desktop keeps its filters always visible"
    assert t.parent is fold and fold.children[0] is t, "the toggle leads the fold"
    assert t.attrs.get("data-click") == "toggleExpanded"
    assert t.attrs.get("data-toggle-target") == ".kf-fold"
    assert t.attrs.get("aria-expanded") == "false"
    assert "Filters" in t.all_text()
    counts = t.find_all("kf-fold-count")
    assert len(counts) == 1 and counts[0].attrs.get("id")
    # Every filter row is still a direct child of the fold.
    assert fold.find_all(row_cls) and all(r.parent is fold for r in fold.find_all(row_cls))


@pytest.mark.parametrize("name,fn", [
    ("intel_kills.html", "applyToChips"),
    ("intel_kills_search.html", "applyChipsToDom"),
], ids=["feed", "search"])
def test_filter_count_is_kept_in_step_with_the_filter_state(name, fn):
    """The count is client state (localStorage / URL), so the page script
    writes it: on load and whenever the chips or chip lists change."""
    src = source(name)
    assert "function updateFilterCount()" in src
    body = src.split(f"function {fn}()", 1)[1].split("\n    }\n", 1)[0]
    assert "updateFilterCount();" in body
    persist = src.split("function persist()", 1)[1].split("\n    }\n", 1)[0]
    assert "updateFilterCount();" in persist


def _fold_observer(name):
    """The IIFE that keeps the fold toggle's aria-expanded in step, and only
    that: the rest of intel_kills.html also sets aria-expanded (the top-strip
    toggle), which would let a broken observer pass."""
    src = source(name)
    marker = "// The toggle's aria-expanded follows the fold's is-expanded class"
    assert src.count(marker) == 1
    return src.split(marker, 1)[1].split("})();", 1)[0]


@pytest.mark.parametrize("name", ["intel_kills.html", "intel_kills_search.html"],
                         ids=["feed", "search"])
def test_fold_toggle_aria_expanded_follows_the_fold(name):
    """toggleExpanded (actions.js) flips .is-expanded on the fold; a
    MutationObserver on the fold's class mirrors it onto the toggle."""
    body = _fold_observer(name)
    assert "document.querySelector('.kf-fold')" in body
    assert "fold.querySelector('.kf-fold-toggle')" in body
    assert "new MutationObserver(" in body
    assert "btn.setAttribute('aria-expanded', fold.classList.contains('is-expanded') ? 'true' : 'false')" in body
    assert ".observe(fold, { attributes: true, attributeFilter: ['class'] })" in body
    # A misspelt feature check would return early every time, and a missing
    # trailing () would leave the observer never set up.
    assert "if (!btn || !window.MutationObserver) return;" in body
    assert body.rstrip().endswith("attributeFilter: ['class'] });")


def test_search_filter_count_leaves_out_sort_and_modes():
    src = source("intel_kills_search.html")
    fn = src.split("function updateFilterCount()", 1)[1].split("\n    }\n", 1)[0]
    for key in ("time", "time_start", "time_end", "space", "wh_class", "category", "count",
                "isk", "primetime", "flags", "ship_ids", "attacker_items", "either_items",
                "victim_items"):
        assert f"state.{key}" in fn, key
    for key in ("sort", "direction", "live", "attacker_mode", "either_mode", "victim_mode"):
        assert f"state.{key}" not in fn, key


def test_feed_filter_count_reads_every_filter():
    src = source("intel_kills.html")
    fn = src.split("function updateFilterCount()", 1)[1].split("\n    }\n", 1)[0]
    for key in ("space", "wh_class", "shattered", "ship_ids", "attacker_ids", "victim_ids"):
        assert f"state.{key}" in fn, key


def test_fold_css():
    phone, _ = phone_block(_section("T1"))
    hide = rule_bodies(phone, ".kf-fold:not(.is-expanded) > :not(.kf-fold-toggle)")
    assert re.search(r"display:\s*none\s*!important", hide)
    toggle = rule_bodies(phone, ".kf-fold > .kf-fold-toggle")
    assert re.search(r"min-height:\s*44px", toggle)
    assert re.search(r"width:\s*100%", toggle)


@pytest.mark.parametrize("sel", [
    ".kf-fold .kf-chip", ".kf-fold .kfs-chip",
    ".kf-fold .kf-chip-removable", ".kf-fold .kfs-chip-removable",
    ".kf-fold .kf-ac-result", ".kf-fold .kfs-ac-result",
])
def test_chips_and_results_are_40px_inside_the_fold(sel):
    phone, _ = phone_block(_section("T1"))
    assert re.search(r"min-height:\s*40px", rule_bodies(phone, sel)), sel


@pytest.mark.parametrize("sel", [".kf-fold .kf-chip-removable .x", ".kf-fold .kfs-chip-removable .x"])
def test_removable_chip_x_has_a_40px_hit_area(sel):
    body = rule_bodies(phone_block(_section("T1"))[0], sel)
    assert re.search(r"min-width:\s*40px", body) and re.search(r"min-height:\s*40px", body)


def test_mode_buttons_are_40px_wide():
    body = rule_bodies(phone_block(_section("T1"))[0], ".kf-fold .kfs-mode-btn")
    assert re.search(r"min-width:\s*40px", body)


@pytest.mark.parametrize("ac,inp,res", [
    (".kf-fold .kf-ac", ".kf-fold .kf-ac-input", ".kf-fold .kf-ac-results"),
    (".kf-fold .kfs-ac", ".kf-fold .kfs-ac-input", ".kf-fold .kfs-ac-results"),
])
def test_autocomplete_fits_at_360(ac, inp, res):
    """The input takes the row's width and its dropdown spans the input,
    so a 200/240px min-width results box can't overhang the viewport."""
    phone, _ = phone_block(_section("T1"))
    assert re.search(r"flex:\s*1 1 100%", rule_bodies(phone, ac))
    assert re.search(r"width:\s*100%", rule_bodies(phone, inp))
    body = rule_bodies(phone, res)
    assert re.search(r"min-width:\s*0", body) and re.search(r"right:\s*0", body)


# ── 5. most-valuable cards ─────────────────────────────────────────────

def test_top_cards_keep_their_image_size_attributes():
    imgs = _dom(_top()).find_all("kf-top-img")
    assert len(imgs) == 12
    for img in imgs:
        assert img.attrs.get("width") == "96" and img.attrs.get("height") == "96"


def _top_card_fetch():
    """bindTopCards' fetch handler: from the fetch to its .catch."""
    src = source("intel_kills.html")
    body = src.split("function bindTopCards()", 1)[1]
    return body.split("fetch('/intel/kills/' + kid + '/detail')", 1)[1].split(".catch(", 1)[0]


def test_top_card_detail_scrolls_into_view_on_phones_only():
    """The detail slot sits below both card grids, far under a top card on a
    phone, so a tap there would seem to do nothing. Phones scroll the slot
    to the top once the panel is in; desktop never scrolls."""
    body = _top_card_fetch()
    assert body.index("slot.innerHTML = html;") < body.index("slot.scrollIntoView(")
    assert body.count("scrollIntoView(") == 1
    gate = "if (window.matchMedia && window.matchMedia('(max-width: 640px)').matches) {"
    assert body.count(gate) == 1
    # The scroll is the gate's whole body.
    inside = body.split(gate, 1)[1].split("\n                  }", 1)[0]
    assert "slot.scrollIntoView(" in inside
    assert "block: 'start'" in inside
    assert "window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'" in inside
    assert "scrollIntoView(" not in body.replace(inside, "")


def test_top_card_detail_scroll_clears_the_sticky_nav():
    """The 46px nav is sticky; the scroll stops 56px down, as R4's LP offers
    and R6's fitting stats do."""
    phone, _ = phone_block(_section("T1"))
    assert re.search(r"scroll-margin-top:\s*56px", rule_bodies(phone, "#kf-top-detail-slot"))


def test_top_cards_css_two_columns_smaller_render():
    """The template's own <style> loads after site.css, so the overrides
    need two classes to win."""
    phone, _ = phone_block(_section("T1"))
    grid = rule_bodies(phone, ".kf-top-section > .kf-top-grid")
    assert re.search(r"grid-template-columns:\s*repeat\(2, ?minmax\(0, ?1fr\)\)", grid)
    img = rule_bodies(phone, ".kf-top-card > .kf-top-img")
    assert re.search(r"width:\s*64px", img) and re.search(r"height:\s*64px", img)


# ── 6. small controls ─────────────────────────────────────────────────

def test_advanced_search_link_is_a_tap_target():
    head = _dom(_feed_page()).find("kf-head")
    links = head.find_all(tag="a", href="/intel/kills/search")
    assert len(links) == 1 and "m-tap" in links[0].classes


def test_search_back_link_centres_its_label_in_the_reset_height():
    """The actions row stretches the link to the 44px Reset button's
    height; flex centres its label there."""
    phone, _ = phone_block(_section("T1"))
    body = rule_bodies(phone, ".kfs-head-actions > a.kfs-btn")
    assert re.search(r"display:\s*flex", body) and re.search(r"align-items:\s*center", body)
    src = source("intel_kills_search.html")
    assert re.search(r'<div class="kfs-head-actions">\s*<button type="button" class="kfs-btn" id="kfs-reset">'
                     r'Reset</button>\s*<a class="kfs-btn" href="/intel/kills"', src)


def test_new_pill_margin_cancels_its_44px_phone_height():
    """The page's -28px fits its 28px desktop pill; the phone button rule
    makes it 44px, so -28px pushed the feed down 16px when it showed."""
    phone, _ = phone_block(_section("T1"))
    assert re.search(r"margin-bottom:\s*-44px", rule_bodies(phone, "button#kf-new-pill"))
    src = source("intel_kills.html")
    assert "margin:8px auto -28px" in src, "the desktop rule this offsets"
    assert '<button type="button" id="kf-new-pill"' in src


# ── desktop stays identical ───────────────────────────────────────────

def test_section_has_no_desktop_rule():
    _, desktop = phone_block(_section("T1"))
    assert desktop.strip() == ""


@pytest.mark.parametrize("render", [_feed, _search_results, lambda: _detail(14), _feed_page,
                                    _search_page], ids=["feed", "search", "detail", "feed-page",
                                                        "search-page"])
def test_every_new_element_is_phone_only(render):
    """The additions (the phone line, the Show all button, the fold toggle)
    are all m-only, so above 640px they are display:none !important."""
    dom = _dom(render())
    for cls in ("kf-meta-line", "m-showall", "kf-fold-toggle"):
        for n in dom.find_all(cls):
            assert "m-only" in n.classes, cls
