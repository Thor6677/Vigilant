"""Mobile R3 T7: Stockpiles, Skill Farm and Appraisal on phones.

Three small tool pages whose lists become expand-on-tap rows on phones; the
in-row controls (Remove, the Skill Farm's Base SP input) move inside the
opened row. Desktop renders as before (D21).

- Stockpiles (D17 A): key 1 is the item, key 2 the deficit (red when short,
  a muted "—" when covered). Name, Current, Target, Note (when there is
  one) and Remove open below.
- Skill Farm (D18 A): key 1 is the pilot, key 2 the injectors ready now
  with their ISK. Name, Total SP, Unallocated, Base SP (the editable
  input), SP / hour, Next injector, Monthly profit (green/red), Note (when
  there is one) and Remove open below. A pilot without the skills scope or a first
  sync shows that message as key 2. The Total row becomes label/value
  lines, not an m-row. The page re-opens the rows a swap closed.
- Appraisal (D19 A): lead is the item icon, key 1 the item, key 2 the line
  total ("—" with no orders). Name, Qty, Unit price and Volume open below.
  The list stops at 10 with "Show all N". The page is login-only, so its
  rows may toggle.

Fixtures run through the routes' own row builders. Names are invented."""
import functools
import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from app.routes import industry as industry_mod
from app.routes import skill_farm as farm_mod
from app.routes import stockpiles as stock_mod
from app.skillfarm import rows as farm_rows
from app.stockpiles.holdings import build_rows
from tests._mobile import (VOID, assert_mrow, assert_single_value_child, cells_rows,
                           clamps, css_section, mrows, phone_block, render_page,
                           row_keys, row_labelled, row_lead, rule_bodies, selectors)

_section = functools.partial(css_section, release="R3")


def _phone():
    """The phone @media body of this task's site.css section."""
    return phone_block(_section("T7"))[0]


class _Deep(HTMLParser):
    """Every .m-row's direct-child cells, each with all of its descendant
    elements as (tag, attrs), in document order. cells_rows only reaches a
    cell's direct children; the Base SP input sits inside a form."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []   # [tag, cell or None, is_row]
        self.rows = []

    def handle_starttag(self, tag, attrs):
        a = {k: (v if v is not None else "") for k, v in attrs}
        parent = self.stack[-1] if self.stack else None
        cell = None
        if parent is not None and parent[2]:
            cell = {"tag": tag, "attrs": a, "desc": []}
            self.rows[-1].append(cell)
        elif parent is not None and parent[1] is not None:
            cell = parent[1]
            cell["desc"].append((tag, a))
        is_row = "m-row" in a.get("class", "").split()
        if is_row:
            self.rows.append([])
        if tag not in VOID:
            self.stack.append([tag, None if is_row else cell, is_row])

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                return


def _deep(html):
    p = _Deep()
    p.feed(html)
    p.close()
    return p.rows


def _labelled_deep(row):
    return {c["attrs"]["data-m-label"]: c for c in row if "data-m-label" in c["attrs"]}


def _classes(attrs):
    return attrs.get("class", "").split()


def _untagged(row):
    return [c for c in row["cells"]
            if "data-m" not in c["attrs"] and "data-m-label" not in c["attrs"]]


def _cells(html, data):
    """cells_rows, one per data row: a zip over no rows would assert nothing."""
    rows = cells_rows(html)
    assert len(rows) == len(data), f"expected {len(data)} m-rows, found {len(rows)}"
    return zip(rows, data)


def _deep_rows(html, data):
    rows = _deep(html)
    assert len(rows) == len(data), f"expected {len(data)} m-rows, found {len(rows)}"
    return zip(rows, data)


# ── Stockpiles ────────────────────────────────────────────────────────

_SP_NAMES = {101: "Sample Charge L", 102: "Sample Drone II",
             103: "Sample Fuel Block", 104: "Sample Probe"}


def _stock_rows():
    """Four targets, one covered (103), sorted as the route sorts them."""
    targets = [NS(id=11, type_id=101, target_qty=5000, note="Doctrine ammo"),
               NS(id=12, type_id=102, target_qty=200, note=""),
               NS(id=13, type_id=103, target_qty=40, note="Reaction fuel"),
               NS(id=14, type_id=104, target_qty=1000, note="")]
    holdings = {101: 1200, 102: 50, 103: 90, 104: 600}
    rows = build_rows(targets, holdings, _SP_NAMES)
    rows.sort(key=lambda r: (-r["deficit"], r["type_name"].lower()))
    return rows


def _render_stock_rows(rows=None):
    rows = _stock_rows() if rows is None else rows
    return render_page(stock_mod, "partials/stockpile_rows.html",
                       "/tools/stockpiles", rows=rows), rows


def test_stockpile_fixture_has_one_covered_target():
    rows = _stock_rows()
    assert len(rows) == 4
    assert [r["under"] for r in rows].count(False) == 1


def test_stockpile_rows_key_on_item_and_deficit():
    html, rows = _render_stock_rows()
    parsed = assert_mrow(html, min_rows=4)
    assert len(parsed) == 4
    for r in parsed:
        assert r["tag"] == "tr"
        assert r["attrs"].get("data-click") == "toggleMRow"
        assert "m-row--link" not in _classes(r["attrs"])
    for row, data in _cells(html, rows):
        assert ("is-under" in _classes(row["attrs"])) == data["under"]
        k1, k2 = row_keys(row)
        assert k1["text"] == data["type_name"]
        assert "sp-deficit" in _classes(k2["attrs"]), "key 2 keeps the deficit's colour hook"
        assert k2["text"] == (f"{data['deficit']:,}" if data["under"] else "—")
        assert not row_lead(row)
        if data["note"]:
            assert not _untagged(row), "every cell opens with the row"


def test_stockpile_rows_open_to_labelled_lines():
    html, rows = _render_stock_rows()
    for row, data in _cells(html, rows):
        assert_single_value_child(row)
        labelled = row_labelled(row)
        want = ["Name", "Current", "Target"] + (["Note"] if data["note"] else []) + ["Remove"]
        assert list(labelled) == want
        assert "m-only" in _classes(labelled["Name"]["attrs"])
        assert labelled["Name"]["text"] == data["type_name"]
        assert labelled["Current"]["text"] == f"{data['current']:,}"
        assert labelled["Target"]["text"] == f"{data['target_qty']:,}"
        if data["note"]:
            assert labelled["Note"]["text"] == data["note"]
        else:
            # No label, so the phone row hides the empty cell rather than
            # opening to a blank "Note" line.
            (empty,) = _untagged(row)
            assert "sp-note" in _classes(empty["attrs"]) and empty["text"] == ""


def test_stockpile_remove_sits_inside_the_opened_row():
    html, rows = _render_stock_rows()
    for row, data in _deep_rows(html, rows):
        buttons = [(c, d) for c in row for d in c["desc"] if d[0] == "button"]
        assert len(buttons) == 1, "the delete control is the row's only button"
        cell, (_, btn) = buttons[0]
        assert cell["attrs"].get("data-m-label") == "Remove", "it opens with the row, nowhere else"
        assert btn["hx-delete"] == f"/tools/stockpiles/{data['id']}"
        assert btn["hx-target"] == "#sp-rows" and btn["hx-swap"] == "innerHTML"
        assert btn["hx-confirm"] == "Remove this stockpile target?"
        assert {"b-btn", "sp-del", "m-tap"} <= set(_classes(btn)), "a 40px tap target"


def test_stockpile_table_is_an_m_table_with_an_m_head():
    html, _ = _render_stock_rows()
    assert re.search(r'<table class="sp-table m-table">', html)
    assert re.search(r'<thead class="m-head">', html)


def test_stockpile_page_includes_the_tagged_rows_in_the_swap_target():
    rows = _stock_rows()
    html = render_page(stock_mod, "stockpiles.html", "/tools/stockpiles", rows=rows)
    inner = html[html.index('<div id="sp-rows">'):]
    assert len(assert_mrow(inner)) == 4


def test_stockpile_rows_carry_their_target_id():
    """The page's re-open script keys open rows on it across a swap."""
    html, rows = _render_stock_rows()
    assert [r["attrs"].get("data-stock-id") for r in mrows(html)] == [str(r["id"]) for r in rows]


def test_stockpile_page_has_the_reopen_script():
    """Smoke test of the re-open script's wiring only: one nonced script
    on the page names the swap target, both htmx events, the open class
    and the row id, and escapes the id it puts in a selector. Whether a
    Remove or Add actually leaves the other open rows open is browser
    behaviour; the review harness checks that at 360px."""
    html = render_page(stock_mod, "stockpiles.html", "/tools/stockpiles", rows=_stock_rows())
    scripts = re.findall(r'<script nonce="test-nonce">(.*?)</script>', html, re.S)
    page = [s for s in scripts if "sp-rows" in s]
    assert len(page) == 1
    for needle in ("htmx:beforeSwap", "htmx:afterSwap", "is-open", "data-stock-id", "CSS.escape("):
        assert needle in page[0], needle


def test_empty_watchlist_has_no_rows():
    html, _ = _render_stock_rows(rows=[])
    assert not mrows(html)
    assert "No stockpile targets yet" in html


# ── Skill Farm ────────────────────────────────────────────────────────

_NOW = datetime.now(timezone.utc)


def _queue(rate_per_hour):
    """A skill training right now at `rate_per_hour`."""
    hours = 100
    return [{"start_date": (_NOW - timedelta(hours=10)).isoformat(),
             "finish_date": (_NOW + timedelta(hours=hours - 10)).isoformat(),
             "training_start_sp": 0, "level_end_sp": int(rate_per_hour * hours)}]


def _pilot(pid, name, base_sp, summary, queue=None):
    return farm_rows.build_pilot_row(
        pilot_id=pid, character_id=90000000 + pid, character_name=name,
        base_sp=base_sp, summary=summary, skillqueue=queue,
        lsi_price=900_000_000.0, extractor_price=400_000_000.0,
        plex_price=2_000_000.0, sales_tax_pct=3.6, plex_per_month=500)


def _pilots(third="no_scope"):
    """Three pilots: training at a profit with injectors ready; not
    training (a loss and a note, none ready); and one that has a status
    message instead of numbers."""
    return [
        _pilot(1, "Sample Farmer One", 5_500_000,
               {"total_sp": 7_250_000, "unallocated_sp": 0}, _queue(2700)),
        _pilot(2, "Sample Farmer Two", 5_000_000,
               {"total_sp": 5_200_000, "unallocated_sp": 150_000}),
        _pilot(3, "Sample Farmer Three", 5_000_000, None if third == "waiting" else "no_scope"),
    ]


def _farm_ctx(rows):
    totals = farm_rows.totals_row(rows)
    return dict(
        error=None,
        settings=NS(sales_tax_pct=3.6, plex_per_month=500, price_source="sell"),
        eligible=[], rows=rows, totals=totals,
        totals_ready_isk_str=farm_rows.isk_str(totals["ready_isk"]),
        totals_profit_str=farm_rows.isk_str(totals["profit"]),
        extractor_price_str="400.00M ISK", lsi_price_str="900.00M ISK",
        plex_price_str="2.00M ISK", extractor_name="Sample Extractor",
        lsi_name="Sample Injector", plex_name="Sample Plex", default_base_sp=5_000_000)


def _render_farm(rows=None):
    rows = _pilots() if rows is None else rows
    html = render_page(farm_mod, "partials/skill_farm_content.html",
                       "/tools/skill-farm", **_farm_ctx(rows))
    return html, rows


def test_farm_fixture_covers_profit_loss_notes_and_status():
    rows = _pilots()
    ok = [r for r in rows if r["status"] == "ok"]
    assert len(ok) == 2
    assert ok[0]["ready_now"] > 0 and ok[0]["ready_isk"] and ok[0]["profit"] > 0
    assert ok[1]["ready_now"] == 0 and ok[1]["profit"] < 0 and ok[1]["notes"]
    assert rows[2]["status"] == "no_scope"


def test_farm_pilot_rows_key_on_pilot_and_ready_now():
    html, rows = _render_farm()
    parsed = assert_mrow(html, min_rows=3)
    assert len(parsed) == 3, "one m-row per pilot; the notes and Total rows are not m-rows"
    for r, data in zip(parsed, rows):
        assert r["tag"] == "tr"
        assert r["attrs"].get("data-click") == "toggleMRow"
        assert r["attrs"].get("data-pilot-id") == str(data["id"]), "the swap restore's handle"
    for row, data in _cells(html, rows):
        k1, k2 = row_keys(row)
        assert k1["text"] == data["character_name"]
        if data["status"] != "ok":
            continue
        want = f"{data['ready_now']} ready"
        if data["ready_isk"]:
            want += f" · {data['ready_isk_str']}"
        assert k2["text"] == want, "Ready now with its ISK, on one line"
        assert "m-only" in _classes(k2["attrs"]), "phones' one-line copy of the desktop cell"
        (desk,) = _untagged(row)
        assert desk["text"].startswith(str(data["ready_now"])), "desktop's Ready now cell stays as it was"


def test_farm_pilot_rows_open_to_labelled_lines():
    html, rows = _render_farm()
    for row, data in _cells(html, rows):
        if data["status"] != "ok":
            continue
        assert_single_value_child(row)
        labelled = row_labelled(row)
        want = ["Name", "Total SP", "Unallocated", "Base SP", "SP / hour", "Next injector",
                "Monthly profit"] + (["Note"] if data["notes"] else []) + ["Remove"]
        assert list(labelled) == want
        assert "m-only" in _classes(labelled["Name"]["attrs"])
        assert labelled["Name"]["text"] == data["character_name"]
        assert labelled["Total SP"]["text"] == f"{data['total_sp']:,}"
        assert labelled["Unallocated"]["text"] == f"{data['unallocated_sp']:,}"
        assert labelled["SP / hour"]["text"] == f"{data['rate_per_hour']:,.0f}"
        assert labelled["Next injector"]["text"] == data["eta_str"]
        profit = labelled["Monthly profit"]
        assert profit["text"] == data["profit_str"]
        assert ("sf-profit" if data["profit"] > 0 else "sf-loss") in _classes(profit["attrs"])
        if data["notes"]:
            note = labelled["Note"]
            assert "m-only" in _classes(note["attrs"])
            assert note["text"] == " · ".join(data["notes"])


def test_base_sp_input_keeps_its_wiring_inside_a_labelled_cell():
    html, rows = _render_farm()
    for row, data in _deep_rows(html, rows):
        inputs = [(c, d[1]) for c in row for d in c["desc"] if d[0] == "input"]
        if data["status"] != "ok":
            assert not inputs
            continue
        assert len(inputs) == 1
        cell, inp = inputs[0]
        assert cell["attrs"].get("data-m-label") == "Base SP"
        assert inp["name"] == "base_sp" and inp["type"] == "number"
        assert "sf-input" in _classes(inp)
        assert inp["value"] == str(data["base_sp"])
        assert (inp["min"], inp["max"], inp["step"]) == ("0", "1000000000", "1")
        (form,) = [d[1] for d in cell["desc"] if d[0] == "form"]
        assert form["hx-post"] == f"/tools/skill-farm/pilots/{data['id']}/base-sp"
        assert form["hx-target"] == "#skill-farm-content" and form["hx-swap"] == "innerHTML"
        assert form["hx-trigger"] == "change, submit"
        assert "data-click" not in inp and "data-click" not in form


def test_farm_remove_sits_inside_the_opened_row():
    html, rows = _render_farm()
    for row, data in _deep_rows(html, rows):
        buttons = [(c, d[1]) for c in row for d in c["desc"] if d[0] == "button"]
        assert len(buttons) == 1
        cell, btn = buttons[0]
        assert cell["attrs"].get("data-m-label") == "Remove"
        assert btn["hx-delete"] == f"/tools/skill-farm/pilots/{data['id']}"
        assert btn["hx-target"] == "#skill-farm-content"
        assert btn["hx-confirm"] == "Remove this farm pilot?"
        assert {"b-btn", "sf-del", "m-tap"} <= set(_classes(btn))


@pytest.mark.parametrize("status,message", [("no_scope", "Needs the skills permission"),
                                            ("waiting", "Waiting for first sync")])
def test_status_rows_show_their_message_as_key_2(status, message):
    html, rows = _render_farm(_pilots(third=status))
    row = cells_rows(html)[2]
    assert rows[2]["status"] == status
    k1, k2 = row_keys(row)
    assert k1["text"] == "Sample Farmer Three"
    assert k2["text"] == message
    assert list(row_labelled(row)) == ["Name", "Remove"]
    assert not _untagged(row)


def test_notes_rows_are_desktop_only():
    """The note under a pilot opens with its row on phones (the m-only Note
    cell); the separate desktop line would otherwise show as a block."""
    html, rows = _render_farm()
    notes_rows = re.findall(r'<tr([^>]*)>\s*<td colspan="9" class="sf-note">', html)
    assert len(notes_rows) == sum(1 for r in rows if r.get("notes"))
    assert notes_rows
    for attrs in notes_rows:
        assert 'class="m-hide"' in attrs and "m-row" not in attrs


def _tfoot(html):
    return html[html.index("<tfoot>"):html.index("</tfoot>")]


def test_total_row_is_label_value_lines_not_an_m_row():
    html, rows = _render_farm()
    foot = _tfoot(html)
    assert not mrows(foot)
    totals = farm_rows.totals_row(rows)
    lines = dict(re.findall(r'<td[^>]*data-sf-label="([^"]+)"[^>]*>(.*?)</td>', foot, re.S))
    assert list(lines) == ["Ready now", "Monthly profit"]
    assert re.sub(r"<[^>]+>|\s+", " ", lines["Ready now"]).split() == [
        str(totals["ready_now"]), *farm_rows.isk_str(totals["ready_isk"]).split()]
    assert lines["Monthly profit"].strip() == farm_rows.isk_str(totals["profit"])


def test_farm_table_is_an_m_table_with_an_m_head():
    html, _ = _render_farm()
    assert '<table class="sf-table m-table">' in html
    assert '<thead class="m-head">' in html


def test_farm_page_has_the_reopen_script():
    """Smoke test of the re-open script's wiring only: one nonced script,
    after the swap target, names it, both htmx events, the open class and
    the row id, and escapes the id it puts in a selector. Whether a Base SP
    change actually keeps its row open is browser behaviour; the review
    harness checks that at 360px (by the keyboard's Go and by blur)."""
    rows = _pilots()
    html = render_page(farm_mod, "skill_farm.html", "/tools/skill-farm", **_farm_ctx(rows))
    inner = html[html.index('<div id="skill-farm-content">'):]
    assert len(assert_mrow(inner)) == 3
    scripts = re.findall(r'<script nonce="test-nonce">(.*?)</script>', html, re.S)
    restore = [s for s in scripts if "skill-farm-content" in s]
    assert len(restore) == 1
    for needle in ("htmx:beforeSwap", "htmx:afterSwap", "is-open", "data-pilot-id", "CSS.escape("):
        assert needle in restore[0], needle
    # Content-block placement: after the swap target, before the page ends.
    assert html.index('<div id="skill-farm-content">') < html.index(restore[0])


# ── Appraisal ─────────────────────────────────────────────────────────

def _isk(a):
    """The partial's fmt_isk macro."""
    if a >= 1e12:
        return f"{a / 1e12:.2f}T"
    if a >= 1e9:
        return f"{a / 1e9:.2f}B"
    if a >= 1e6:
        return f"{a / 1e6:.2f}M"
    if a >= 1e3:
        return f"{a / 1e3:.1f}K"
    return f"{a:.0f}"


def _items(n):
    """`n` resolved items built as the route builds them; the fourth has no
    sell orders. Sorted by total, most valuable first."""
    items = []
    for i in range(n):
        qty = (i + 1) * 150
        price = None if i == 3 else 1234.5 * (i + 1) ** 3
        vol = 0.01 * (i + 1)
        items.append({"type_id": 2001 + i, "name": f"Sample Item {i + 1:02d}", "qty": qty,
                      "unit_price": price, "total_price": (price or 0) * qty,
                      "unit_volume": vol, "total_volume": round(vol * qty, 2)})
    items.sort(key=lambda x: x["total_price"], reverse=True)
    return items


def _render_appraisal(n=14):
    items = _items(n)
    html = render_page(industry_mod, "partials/appraisal_results.html", "/industry/appraisal",
                       items=items, unresolved=[], hub_label="Sample Hub",
                       total_isk=sum(i["total_price"] for i in items),
                       total_volume=sum(i["total_volume"] for i in items), item_count=len(items))
    return html, items


def test_appraisal_rows_lead_icon_key_name_and_total():
    html, items = _render_appraisal()
    parsed = assert_mrow(html, min_rows=14)
    assert len(parsed) == 14, "item rows only: not the header or Total row"
    for r, item in zip(parsed, items):
        assert r["attrs"].get("data-click") == "toggleMRow"
        assert r["attrs"]["data-haul-name"] == item["name"], "Send to Hauling still reads the rows"
        assert r["attrs"]["data-haul-qty"] == str(item["qty"])
        assert r["attrs"]["data-haul-volume"] == str(item["unit_volume"])
    for row, item in _cells(html, items):
        (lead,) = row_lead(row)
        assert lead["tag"] == "img" and "m-only" in _classes(lead["attrs"])
        assert f"/types/{item['type_id']}/icon" in lead["attrs"]["src"]
        assert (lead["attrs"]["width"], lead["attrs"]["height"]) == ("16", "16")
        # Never hide a tagged cell inline: the phone rules' !important beats
        # it anyway, so a broken icon just leaves its empty 16x16 slot.
        assert "data-on-error" not in lead["attrs"]
        k1, k2 = row_keys(row)
        assert k1["text"] == item["name"]
        icons = [k for k in k1["kids"] if "/icon" in k.get("src", "")]
        assert len(icons) == 1 and "m-hide" in _classes(icons[0]), "the desktop icon hides on phones"
        assert k2["text"] == (_isk(item["total_price"]) if item["unit_price"] is not None else "—")


def test_appraisal_rows_open_to_labelled_lines():
    html, items = _render_appraisal()
    for row, item in _cells(html, items):
        assert_single_value_child(row)
        labelled = row_labelled(row)
        assert list(labelled) == ["Name", "Qty", "Unit price", "Volume"]
        assert "m-only" in _classes(labelled["Name"]["attrs"])
        assert labelled["Name"]["text"] == item["name"]
        assert labelled["Qty"]["text"] == f"{item['qty']:,}"
        assert labelled["Unit price"]["text"] == (
            f"{item['unit_price']:,.2f}" if item["unit_price"] is not None else "No orders")
        assert labelled["Volume"]["text"] == f"{item['total_volume']:,.1f} m³"
        assert not _untagged(row)


def test_appraisal_list_shows_all_past_10():
    html, _ = _render_appraisal(14)
    c = clamps(html)
    assert c.wraps == 1 and c.nested_wraps == 0
    (clamp,) = c.clamps
    assert clamp["children"] == 14
    assert "m-clamp-wrap" not in clamp["classes"], "Show all must not open every row"
    (btn,) = c.showall
    assert btn["text"] == "Show all 14" and btn["in_wrap"]
    assert btn["attrs"]["data-click"] == "toggleExpanded"
    assert btn["attrs"]["data-toggle-target"] == ".m-clamp-wrap"
    assert {"m-only", "m-showall"} <= set(_classes(btn["attrs"]))
    assert btn["attrs"].get("type") == "button"
    # Send to Hauling's scope is the clamped list itself, so it still
    # collects every row, shown or not.
    assert re.search(r'<div id="appraisal-items" class="m-clamp">', html)


def test_appraisal_at_10_items_has_no_show_all():
    html, _ = _render_appraisal(10)
    c = clamps(html)
    assert c.clamps[0]["children"] == 10
    assert not c.showall


def test_appraisal_header_is_m_head_and_total_row_is_plain():
    html, items = _render_appraisal()
    head = re.search(r'<div class="([^"]*)"[^>]*>\s*<span[^>]*>Item</span>', html)
    assert head and "m-head" in head.group(1).split()
    total = re.search(r'<div class="([^"]*)"[^>]*>\s*<span[^>]*>Total</span>', html)
    assert total and "m-row" not in total.group(1).split()


def test_appraisal_total_row_drops_its_spacers_on_phones():
    """The two empty flex:1 spacers are m-hide, so on phones the total and
    the volume get their width (site.css keeps them on one line)."""
    html, _ = _render_appraisal()
    m = re.search(r'<div class="([^"]*)"[^>]*>\s*<span[^>]*>Total</span>(.*?)</div>', html, re.S)
    assert m and "apr-total" in m.group(1).split()
    spans = re.findall(r'<span([^>]*)>(.*?)</span>', m.group(2), re.S)
    empty = [a for a, t in spans if not t.strip()]
    values = [a for a, t in spans if t.strip()]
    assert len(empty) == 2 and all('class="m-hide"' in a and "flex:1" in a for a in empty)
    assert len(values) == 2 and not any("m-hide" in a for a in values)


def test_appraisal_page_is_login_only_so_its_rows_may_toggle():
    """actions.js only loads for a session, so a page a stranger can open
    must carry no data-click. The appraisal page sends strangers home."""
    import app.main as main
    r = TestClient(main.app).get("/industry/appraisal", follow_redirects=False)
    assert r.status_code in (302, 303, 307)
    assert r.headers["location"] == "/"


# ── CSS (this task's site.css section) ───────────────────────────────

def test_css_section_is_phone_only():
    assert phone_block(_section("T7"))[1].strip() == ""


def _decls(body):
    return {k.strip(): v.strip() for k, v in
            (d.split(":", 1) for d in body.split(";") if ":" in d)}


_FARM = "#skill-farm-content .sf-table"


@pytest.mark.parametrize("table", [".sp-table", _FARM])
def test_css_table_rows_draw_one_rule_and_cells_drop_their_desktop_box(table):
    phone = _phone()
    row = _decls(rule_bodies(phone, f"{table} tr.m-row"))
    assert row["border-bottom"] == "1px solid var(--border)"
    cell = _decls(rule_bodies(phone, f"{table} tr.m-row > td"))
    assert cell["padding"] == "0" and cell["border-bottom"] == "none"


def test_css_stockpile_tint_moves_to_the_row_and_covered_dash_is_muted():
    phone = _phone()
    assert "color-mix(in srgb, var(--danger) 12%, transparent)" in _decls(
        rule_bodies(phone, ".sp-table tr.m-row.is-under"))["background"]
    # Beats the page's `.sp-table tr.is-under td` (0,2,2), which loads later.
    assert _decls(rule_bodies(phone, ".sp-table tr.m-row.is-under > td"))["background"] == "none"
    assert _decls(rule_bodies(phone, ".sp-table tr.m-row:not(.is-under) > .sp-deficit"))[
        "color"] == "var(--muted)"


@pytest.mark.parametrize("sel", [".sp-table tr.m-row > td > .sp-del",
                                 f"{_FARM} tr.m-row > td > .sf-del"])
def test_css_remove_keeps_its_40px_in_an_open_row(sel):
    """.b-btn is flex:1, which would stretch the × across the open row."""
    assert _decls(rule_bodies(_phone(), sel))["flex"] == "none"


def test_css_base_sp_input_is_wider_in_an_opened_row():
    """The desktop 100px box holds 7 digits at 12px; phones force 16px."""
    body = _decls(rule_bodies(_phone(), f"{_FARM} tr.m-row > [data-m-label] .sf-input"))
    assert body["width"] == "10em"


def test_css_monthly_profit_is_green_or_red_on_phones():
    """The page's `.sf-table td` colour outranks `.sf-profit` / `.sf-loss`,
    so desktop draws profit in the text colour. Phones show the green/red
    (D18 A) in open rows and on the Total line; desktop stays as it is."""
    phone = _phone()
    assert _decls(rule_bodies(phone, f"{_FARM} td.sf-profit"))["color"] == "#93c47d"
    assert _decls(rule_bodies(phone, f"{_FARM} td.sf-loss"))["color"] == "#e06666"


def test_css_total_row_becomes_label_value_lines():
    phone = _phone()
    for sel in (f"{_FARM} > tfoot", f"{_FARM} > tfoot > tr"):
        assert _decls(rule_bodies(phone, sel))["display"] == "block", sel
    assert _decls(rule_bodies(phone, f"{_FARM} > tfoot > tr"))["border-top"] == "2px solid var(--border)"
    assert _decls(rule_bodies(phone, f"{_FARM} > tfoot > tr > td"))["display"] == "none"
    line = _decls(rule_bodies(phone, f"{_FARM} > tfoot > tr > td[data-sf-label]"))
    assert line["display"] == "block" and line["border-top"] == "none"
    label = _decls(rule_bodies(phone, f"{_FARM} > tfoot > tr > td[data-sf-label]::before"))
    assert label["content"] == "attr(data-sf-label)" and label["float"] == "left"


def test_css_skill_farm_rules_are_scoped_to_the_page():
    """fitting_saved.html also has a .sf-table, which R6 turns into m-rows
    in the same release. Every Skill Farm selector here starts at this
    page's swap target, so neither page's phone rules reach the other.
    The ID also outranks the page's later <style>, so nothing needs
    !important."""
    section = _section("T7")
    sels = [s for m in re.finditer(r"([^{}]+)\{[^{}]*\}", _phone()) for s in selectors(m.group(1))]
    farm = [s for s in sels if "sf-" in s]
    assert len(farm) >= 10
    for s in farm:
        assert s.startswith(_FARM + " "), s
    assert "!important" not in section


def test_css_appraisal_total_values_stay_on_one_line():
    assert _decls(rule_bodies(_phone(), ".apr-total > span"))["white-space"] == "nowrap"


def test_css_appraisal_show_all_is_inset_in_its_panel():
    body = _decls(rule_bodies(_phone(), ".apr-clamp > .m-showall"))
    assert body["width"] == "calc(100% - 1.5rem)"
