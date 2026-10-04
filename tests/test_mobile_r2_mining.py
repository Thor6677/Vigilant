"""Mobile R2 T4: the mining ledger's four lists on phones.

`mining.html` renders both `/character/{id}/mining` and the aggregated
`/corporations/{id}/mining` (with an "Includes:" chip row and no character
tabs). On phones By Ore Type, By System, Daily Summary and Full Detail are
expand-on-tap rows keyed on name/date (key 1) and ISK value (key 2, user
decision D7 A). Daily Summary and Full Detail stop at 10 rows with a
"Show all N" button (D8 A); Full Detail's clamp sits inside the body its
panel header shows and hides, so that toggle still works. Desktop renders
as before (D21).

The context is built the way the routes build it: raw ledger rows run
through `_aggregate_ledger`. Names and ids are invented."""
import re
from html.parser import HTMLParser

import pytest

from app.routes import mining as mining_mod
from tests._mobile import (VOID, assert_mrow, assert_single_value_child, cells_rows,
                           clamps, css_section, render_page, row_keys, row_labelled,
                           row_lead, rule_bodies, SITE_CSS)

_CHAR = {"character_id": 90000001, "character_name": "Sample Miner",
         "corporation_id": 98000001, "corporation_name": "Sample Mining Corp"}
_ORES = {1001 + i: f"Sample Ore {name}" for i, name in enumerate("ABCDEF")}
_PRICES = {tid: 100.0 * (n + 1) for n, tid in enumerate(_ORES)}
_SYSTEMS = {30000001: "Sample System One", 30000002: "Sample System Two",
            30000003: "Sample System Three"}


def _raw(days):
    """One ledger row per day, cycling through six ores and three systems,
    so `days` rows give `days` dates and `days` detail entries."""
    tids, sids = list(_ORES), list(_SYSTEMS)
    return [{"date": f"2026-09-{d + 1:02d}", "type_id": tids[d % 6],
             "solar_system_id": sids[d % 3], "quantity": 25000 + 1500 * d}
            for d in range(days)]


def _data(days):
    return mining_mod._aggregate_ledger(_raw(days), _ORES, _SYSTEMS, _PRICES)


def _render_char(days=14):
    data = _data(days)
    html = render_page(mining_mod, "mining.html", "/character/90000001/mining",
                       char=dict(_CHAR), data=data, error=None,
                       is_corp=False, corp_id=None, characters=[])
    return html, data


def _render_corp(days=14):
    """The corp route: two contributing characters, their ledgers merged."""
    data = _data(days)
    char = {k: _CHAR[k] for k in ("character_id", "character_name", "corporation_name")}
    html = render_page(mining_mod, "mining.html", "/corporations/98000001/mining",
                       char=char, data=data, error=None, is_corp=True,
                       corp_id=98000001, characters=["Sample Miner", "Sample Hauler"])
    return html, data


_VARIANTS = {"character": _render_char, "corp": _render_corp}


def _isk(amount):
    """The template's format_isk macro."""
    if amount >= 1e9:
        return f"{amount / 1e9:.2f}B"
    if amount >= 1e6:
        return f"{amount / 1e6:.2f}M"
    if amount >= 1e3:
        return f"{amount / 1e3:.1f}K"
    return f"{amount:.0f}"


def _lists(html, data):
    """The rendered m-rows split into the four lists, in page order."""
    rows = cells_rows(html)
    sizes = [("ore", len(data["by_ore"])), ("system", len(data["by_system"])),
             ("daily", len(data["by_date"])), ("detail", len(data["entries"]))]
    assert len(rows) == sum(n for _, n in sizes)
    out, at = {}, 0
    for name, n in sizes:
        out[name], at = rows[at:at + n], at + n
    return out


def _untagged(row):
    return [c for c in row["cells"]
            if "data-m" not in c["attrs"] and "data-m-label" not in c["attrs"]]


def _pct(value, total):
    return f"{value / total * 100:.0f}%"


# ── Rows ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("variant", _VARIANTS)
def test_all_four_lists_are_tap_to_open_rows(variant):
    html, data = _VARIANTS[variant]()
    rows = assert_mrow(html, min_rows=6 + 3 + 14 + 14)
    assert len(rows) == 6 + 3 + 14 + 14
    for r in rows:
        assert r["attrs"].get("data-click") == "toggleMRow"
        assert "m-row--link" not in r["attrs"]["class"].split()
    for r in cells_rows(html):
        assert_single_value_child(r)
        assert not row_lead(r), "ore icons stay inside key 1, not leads"


@pytest.mark.parametrize("variant", _VARIANTS)
def test_by_ore_type_rows(variant):
    html, data = _VARIANTS[variant]()
    rows = _lists(html, data)["ore"]
    total = data["total_value"]
    for row, ore in zip(rows, data["by_ore"]):
        k1, k2 = row_keys(row)
        assert k1["text"] == ore["name"]
        imgs = [k for k in k1["kids"] if f"/types/{ore['type_id']}/icon" in k.get("src", "")]
        assert len(imgs) == 1, "the ore icon stays inline in key 1"
        assert "width:20px;height:20px" in imgs[0]["style"]
        assert "mining-ore" in k1["attrs"]["class"].split()
        assert k2["text"] == _isk(ore["value"])
        labelled = row_labelled(row)
        assert list(labelled) == ["Units", "Share"]
        assert labelled["Units"]["text"] == f"{ore['quantity']:,.0f}"
        assert labelled["Share"]["text"] == _pct(ore["value"], total)
        assert not _untagged(row)


@pytest.mark.parametrize("variant", _VARIANTS)
def test_by_system_rows(variant):
    html, data = _VARIANTS[variant]()
    rows = _lists(html, data)["system"]
    total = data["total_value"]
    for row, sys in zip(rows, data["by_system"]):
        k1, k2 = row_keys(row)
        assert k1["text"] == sys["name"]
        assert k2["text"] == _isk(sys["value"])
        labelled = row_labelled(row)
        assert list(labelled) == ["Units", "Share"]
        assert labelled["Units"]["text"] == f"{sys['quantity']:,.0f}"
        assert labelled["Share"]["text"] == _pct(sys["value"], total)
        assert not _untagged(row)


@pytest.mark.parametrize("variant", _VARIANTS)
def test_daily_summary_rows_leave_the_bar_untagged(variant):
    html, data = _VARIANTS[variant]()
    rows = _lists(html, data)["daily"]
    for row, day in zip(rows, data["by_date"]):
        k1, k2 = row_keys(row)
        assert k1["text"] == day["date"]
        assert k2["text"] == _isk(day["value"])
        labelled = row_labelled(row)
        assert list(labelled) == ["Units", "Types"]
        assert labelled["Units"]["text"] == f"{day['quantity']:,.0f}"
        assert labelled["Types"]["text"] == str(day["types"])
        bar = _untagged(row)
        assert len(bar) == 1, "only the bar cell is untagged, so phones hide it"
        assert any("height:4px" in k.get("style", "") for k in bar[0]["kids"])


@pytest.mark.parametrize("variant", _VARIANTS)
def test_full_detail_rows(variant):
    html, data = _VARIANTS[variant]()
    rows = _lists(html, data)["detail"]
    for row, e in zip(rows, data["entries"]):
        k1, k2 = row_keys(row)
        assert k1["text"] == e["ore_name"]
        imgs = [k for k in k1["kids"] if f"/types/{e['type_id']}/icon" in k.get("src", "")]
        assert len(imgs) == 1, "the ore icon stays inline in key 1"
        assert "width:16px;height:16px" in imgs[0]["style"]
        assert "mining-ore" in k1["attrs"]["class"].split()
        assert k2["text"] == _isk(e["value"])
        labelled = row_labelled(row)
        assert list(labelled) == ["Date", "System", "Quantity"]
        assert labelled["Date"]["text"] == e["date"]
        assert labelled["System"]["text"] == e["system_name"]
        assert labelled["Quantity"]["text"] == f"{e['quantity']:,.0f}"
        assert not _untagged(row)


# ── Structure: headers, clamps, the toggled body ─────────────────────

class _Tree(HTMLParser):
    """Every start tag with its attrs and the attrs of its open ancestors."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.tags = [], []

    def handle_starttag(self, tag, attrs):
        a = {k: (v if v is not None else "") for k, v in attrs}
        self.tags.append((tag, a, [s[1] for s in self.stack]))
        if tag not in VOID:
            self.stack.append((tag, a))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                return


def _tree(html):
    p = _Tree()
    p.feed(html)
    p.close()
    return p.tags


def _cls(a):
    return a.get("class", "").split()


@pytest.mark.parametrize("variant", _VARIANTS)
def test_every_table_row_is_an_m_row_or_an_m_head(variant):
    html, _ = _VARIANTS[variant]()
    table_rows = [a for _, a, _ in _tree(html) if "b-table-row" in _cls(a)]
    heads = [a for a in table_rows if "m-head" in _cls(a)]
    assert len(heads) == 2, "Daily Summary and Full Detail each have one header row"
    for a in table_rows:
        assert "m-row" in _cls(a) or "m-head" in _cls(a), a


@pytest.mark.parametrize("variant", _VARIANTS)
def test_daily_summary_and_full_detail_show_all_past_ten(variant):
    html, data = _VARIANTS[variant]()
    c = clamps(html)
    assert c.wraps == 2 and c.nested_wraps == 0
    assert [k["children"] for k in c.clamps] == [len(data["by_date"]), len(data["entries"])] == [14, 14]
    assert [b["text"] for b in c.showall] == ["Show all 14", "Show all 14"]
    for b in c.showall:
        assert b["in_wrap"]
        assert {"m-only", "m-showall"} <= set(_cls(b["attrs"]))
        assert b["attrs"].get("type") == "button"
        assert b["attrs"].get("data-click") == "toggleExpanded"
        assert b["attrs"].get("data-toggle-target") == ".m-clamp-wrap"
    for _, a, anc in _tree(html):
        if "m-showall" in _cls(a):
            assert {"m-clamp-wrap", "mining-clamp"} <= set(_cls(anc[-1])), (
                "the button is a direct child of its page-scoped wrap")


@pytest.mark.parametrize("variant", _VARIANTS)
def test_no_show_all_at_ten_rows(variant):
    html, data = _VARIANTS[variant](days=10)
    c = clamps(html)
    assert [k["children"] for k in c.clamps] == [10, 10]
    assert c.showall == []


def test_show_all_counts_follow_each_list():
    """Daily Summary counts days, Full Detail counts entries: two rows on
    one day make 12 entries over 11 days."""
    raw = _raw(11) + [{"date": "2026-09-01", "type_id": 1002, "solar_system_id": 30000002,
                       "quantity": 5000}]
    data = mining_mod._aggregate_ledger(raw, _ORES, _SYSTEMS, _PRICES)
    html = render_page(mining_mod, "mining.html", "/character/90000001/mining",
                       char=dict(_CHAR), data=data, error=None,
                       is_corp=False, corp_id=None, characters=[])
    assert [b["text"] for b in clamps(html).showall] == ["Show all 11", "Show all 12"]


@pytest.mark.parametrize("variant", _VARIANTS)
def test_full_detail_clamp_sits_inside_the_toggled_body(variant):
    html, data = _VARIANTS[variant]()
    tags = _tree(html)
    heads = [a for _, a, _ in tags if a.get("data-toggle-panel") == "detail-table"]
    assert len(heads) == 1 and heads[0].get("data-click") == "togglePanel"
    body = [a for _, a, _ in tags if a.get("id") == "detail-table"]
    assert len(body) == 1 and body[0].get("style", "").replace(" ", "") == "display:none;"
    wraps = [anc for _, a, anc in tags if "m-clamp-wrap" in _cls(a)]
    assert len(wraps) == 2
    assert not any(x.get("id") == "detail-table" for x in wraps[0]), "Daily Summary isn't toggled"
    assert any(x.get("id") == "detail-table" for x in wraps[1])
    detail_rows = [anc for _, a, anc in tags
                   if "m-row" in _cls(a) and any(x.get("id") == "detail-table" for x in anc)]
    assert len(detail_rows) == len(data["entries"])
    for anc in detail_rows:
        assert "m-clamp" in _cls(anc[-1]), "detail rows are direct children of the clamp"


@pytest.mark.parametrize("variant", _VARIANTS)
def test_rows_and_tagged_cells_are_never_hidden_inline(variant):
    """The phone rules use display:… !important, which would override an
    inline display:none on a row or tagged cell; only wrappers hide."""
    html, _ = _VARIANTS[variant]()
    for r in cells_rows(html):
        for a in [r["attrs"]] + [c["attrs"] for c in r["cells"] if c["attrs"].keys()
                                 & {"data-m", "data-m-label"}]:
            assert "display:none" not in a.get("style", "").replace(" ", "")
            assert "hidden" not in a


def test_corp_variant_keeps_its_chips_and_has_no_character_tabs():
    html, _ = _render_corp()
    assert "Includes:" in html and "Sample Hauler" in html
    assert "b-tab-strip" not in html and "m-tabs" not in html
    char_html, _ = _render_char()
    assert "b-tab-strip" in char_html


# ── CSS ───────────────────────────────────────────────────────────────

def test_ore_key_keeps_its_icon_beside_an_ellipsised_name():
    """Key 1 is display:block on phones, which would drop the icon to the
    text baseline and lose the gap. The ore cell stays a flex line: the
    icon keeps its size and only the name ellipsises."""
    css = css_section("T4")
    cell = rule_bodies(css, '.m-row > [data-m="key"].mining-ore')
    assert "display: flex !important" in cell and "align-items: center" in cell
    assert "flex: none" in rule_bodies(css, ".m-row > .mining-ore > img")
    name = rule_bodies(css, ".m-row > .mining-ore > span")
    for decl in ("min-width: 0", "overflow: hidden", "text-overflow: ellipsis", "white-space: nowrap"):
        assert decl in name, decl


def test_show_all_is_inset_inside_its_panel():
    body = rule_bodies(css_section("T4"), ".mining-clamp > .m-showall")
    assert "width: calc(100% - 1.5rem)" in body
    assert "margin: 0.4rem 0.75rem 0.6rem" in body


def test_mining_classes_have_no_desktop_rules():
    """mining-ore and mining-clamp only act on phones, so desktop renders
    exactly as before (D21)."""
    with open(SITE_CSS, encoding="utf-8") as fh:
        css = re.sub(r"/\*.*?\*/", "", fh.read(), flags=re.S)
    section = css_section("T4")
    phone_block = section[section.index("{") + 1:section.rindex("}")]
    for cls in (".mining-ore", ".mining-clamp"):
        assert css.count(cls) == phone_block.count(cls) > 0, cls
