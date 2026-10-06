"""Mobile R6 T4: the fitting tool at phone width (≤640px).

  1. Module browser (D9 A). On phones each add from the browser overlay
     shows a short note inside the overlay ("✓ Added X · high 4/5"), worked
     out from the page's own slot state. The overlay stays open. The note
     floats over the list with pointer-events:none, fades, and has its text
     cleared once the fade has run. The close ×
     is a square 44px target, the tree and item rows (divs) are 40px, and a
     cannot-fit item spells out its reason (desktop keeps the hover title).
  2. Pinned summary bar (D10 A). A phone-only button fixed to the bottom
     shows DPS, EHP and Cap from the stats partial's data-* hooks. It is
     refreshed on every #stats-panel write, including the error path, and
     tapping it scrolls to #stats-panel. It hides while the browser overlay,
     a modal or the charge selector is open, and the page gets bottom
     padding so the bar never covers the last controls. Values are 12px
     and keys 11px; extreme values (spool DPS, a capital's EHP) truncate
     with an ellipsis rather than push the bar past the screen edge.
  3. Slot rows (D11 A). The state dot, charge button and remove × are
     40px wide and 44px tall: the global phone button rule gives the height
     and .fit-ctl the 40px width (it never sets a height, which would
     outrank that 44px floor). 40px rather than 44px leaves a high slot's
     name room for "200mm AutoCannon II" at 360px. The `i` button is m-hide, since the module icon opens
     info. A phone-only copy of the name truncates, with the loaded charge
     as a muted second line. The desktop name span is m-hide, untouched.
     Drones, implants and boosters get the same controls.
  4. Toolbars: header buttons centre their labels, the custom damage
     sliders become a 2×2 grid, the stats %/EHP toggles share .fit-ctl
     (never m-tap, whose 40px !important would shrink a button) and the
     stats summary line wraps.
  5. EFT import: on phones the box sits at the top, scrolls inside a
     max-height and keeps Import/Cancel in a sticky row. The info and
     character-import modals' corner × is a square 44px target that a long
     module name keeps clear of.

Desktop renders identically (D21). The new elements are m-only, the new
classes have rules only in this section's phone block, and the JS phone
note is gated on matchMedia. Names and ids are invented."""
import functools
import re
from collections import defaultdict
from html.parser import HTMLParser

import pytest

import app.main  # noqa: F401 — populates every router's templates.env.globals
from app.routes import fitting as fitting_mod
from tests._mobile import (SITE_CSS, VOID, css_section, norm, phone_block,
                           render_page, rule_bodies, selectors)

_section = functools.partial(css_section, release="R6")


# ── helpers ─────────────────────────────────────────────────────────

class _Tree(HTMLParser):
    """Every element as {"tag", "attrs", "cls", "parent", "text"}."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.nodes, self.stack = [], []

    def handle_starttag(self, tag, attrs):
        a = {k: (v if v is not None else "") for k, v in attrs}
        self.nodes.append({"tag": tag, "attrs": a, "cls": a.get("class", "").split(),
                           "parent": self.stack[-1] if self.stack else None, "text": ""})
        if tag not in VOID:
            self.stack.append(len(self.nodes) - 1)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.nodes[self.stack[i]]["tag"] == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        for i in self.stack:
            self.nodes[i]["text"] += data


def _tree(html):
    t = _Tree()
    t.feed(html)
    t.close()
    return t.nodes


def _by_id(nodes, id_):
    hits = [n for n in nodes if n["attrs"].get("id") == id_]
    assert len(hits) == 1, f"expected one #{id_}, found {len(hits)}"
    return hits[0]


def _ancestors(nodes, node):
    out, p = [], node["parent"]
    while p is not None:
        out.append(nodes[p])
        p = nodes[p]["parent"]
    return out


@pytest.fixture(scope="module")
def page():
    return render_page(fitting_mod, "fitting_tool.html", "/tools/fitting", folder_paths=[])


@pytest.fixture(scope="module")
def script(page):
    blocks = re.findall(r"<script nonce=[^>]*>(.*?)</script>", page, re.S)
    body = [b for b in blocks if "function renderSlots" in b]
    assert len(body) == 1
    return body[0]


def _fn(script, name):
    """The body of `function <name>(...) { ... }`, by brace matching. The
    functions read here keep braces out of their string literals."""
    m = re.search(r"function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{", script)
    assert m, f"no function {name}() in the page script"
    depth, i = 1, m.end()
    while depth:
        depth += (script[i] == "{") - (script[i] == "}")
        i += 1
    return script[m.end():i - 1]


def _line(body, needle):
    """The one source line of `body` containing `needle`."""
    hits = [ln for ln in body.splitlines() if needle in ln]
    assert len(hits) == 1, f"expected one line with {needle!r}, found {len(hits)}"
    return hits[0]


def _classes_in(line, binding):
    """The class list of the JS-string element carrying `binding` on `line`."""
    m = re.search(r"<(\w+) class=\"([^\"]*)\"[^<]*?" + re.escape(binding), line)
    assert m, f"no class attribute before {binding!r} in: {line.strip()[:160]}"
    return m.group(2).split()


def _phone():
    return phone_block(_section("T4"))


def _decls(selector):
    body = rule_bodies(_phone()[0], selector)
    assert body.strip(), f"no phone rule for {selector!r} in the R6 T4 section"
    out = {}
    for part in body.split(";"):
        if ":" in part:
            prop, val = part.split(":", 1)
            out[prop.strip()] = norm(val)
    return out


# ── 1. Module browser ───────────────────────────────────────────────

def test_add_note_lives_in_the_browser_panel_and_is_phone_only(page):
    nodes = _tree(page)
    note = _by_id(nodes, "browser-added-note")
    assert "m-only" in note["cls"] and "fit-added-note" in note["cls"]
    assert note["attrs"].get("role") == "status"
    assert note["attrs"].get("aria-live") == "polite"
    assert any(a["attrs"].get("id") == "browser-panel" for a in _ancestors(nodes, note))


def test_add_note_is_phone_gated_and_fades(script):
    assert "window.matchMedia('(max-width: 640px)')" in script
    note = _fn(script, "_noteBrowserAdd")
    assert re.search(r"if \(!_fitIsPhone\(\)\) return;", note), "the note must be phone-only"
    assert "classList.add('is-shown')" in note
    assert "clearTimeout(" in note and "setTimeout(" in note, (
        "each add replaces the note and restarts its fade timer")
    assert "textContent" in note and "innerHTML" not in note


def test_add_note_text_clears_after_the_fade(script):
    # The note is role=status: at opacity 0 it would still read out the
    # last add, so its text goes once the fade has run (not in the same
    # tick, which would cut the fade short).
    note = _fn(script, "_noteBrowserAdd")
    fade = note.index("note.classList.remove('is-shown');")
    clear = re.search(r"_addNoteTimer = setTimeout\(function\(\) \{ note\.textContent = ''; \}, (\d+)\);", note)
    assert clear, "the text is cleared by a timer of its own"
    assert fade < clear.start(), "the clear is scheduled from the fade callback"
    fade_s = re.search(r"opacity ([\d.]+)s", _decls(".fit-added-note")["transition"])
    assert int(clear.group(1)) >= float(fade_s.group(1)) * 1000, "the clear waits out the fade"
    # Both stages share the one timer each add cancels first, so an earlier
    # add's pending clear can never wipe a newer note.
    assert note.count("_addNoteTimer = setTimeout(") == 2
    assert note.index("clearTimeout(_addNoteTimer);") < note.index("_addNoteTimer = setTimeout(")


def test_browser_add_confirms_with_the_rack_fill(script):
    add = _fn(script, "addModuleFromBrowser")
    assert "_noteBrowserAdd(_browserAddNoteText(typeId, typeName, slotType));" in add
    assert "closeModuleBrowser" not in add, "the overlay stays open (D9 A)"
    text = _fn(script, "_browserAddNoteText")
    assert "'✓ Added ' + typeName + ' · ' + where" in text
    fill = _fn(script, "_rackFill")
    assert "getSlotCount(slot)" in fill and "getSlotLimit(slot)" in fill
    assert re.search(r"med:\s*'mid'", script), "rack names read high/mid/low"


def test_full_rack_says_so_instead_of_doing_nothing(script):
    add = _fn(script, "addModuleFromBrowser")
    full = add[add.index("isSlotFull(slotType)"):]
    full = full[:full.index("return;")]
    assert "_noteBrowserAdd(" in full and "_rackFill(slotType)" in full


def test_browser_tree_and_item_rows_carry_the_40px_hook(script):
    tree = _fn(script, "renderBrowseTree")
    rows = [ln for ln in tree.splitlines() if "b-hover-accent" in ln]
    assert len(rows) == 2 and all('class="b-hover-accent fit-brow"' in ln for ln in rows), (
        "the Back row and every group row are 40px on phones")
    items = _fn(script, "renderBrowseItems")
    assert re.search(r"var rowClass = \(canFit \? 'b-hover-accent ' : ''\) \+ 'fit-brow';", items)


def test_cannot_fit_reason_is_visible_text_on_phones(script):
    items = _fn(script, "renderBrowseItems")
    line = _line(items, "Cannot fit this ship\">")
    assert 'title="Cannot fit this ship"' in line, "desktop keeps its hover title"
    assert '<span class="m-only fit-nofit-why">Cannot fit this ship</span>' in line


def test_browser_close_is_a_square_44px_target():
    close = _decls("#browser-panel #browser-close")
    assert close["min-width"] == "44px"
    assert "min-height" not in close, "the global button rule gives it 44px; don't cap it"


def test_browser_rows_and_note_css():
    assert _decls("#browser-panel .fit-brow")["min-height"] == "40px"
    note = _decls(".fit-added-note")
    assert note["position"] == "absolute" and note["bottom"]
    assert note["pointer-events"] == "none", "the note must never block a tap on the list"
    assert note["opacity"] == "0"
    assert _decls(".fit-added-note.is-shown")["opacity"] == "1"
    assert _decls(".fit-nofit-why")["display"] == "block"


# ── 2. Pinned summary bar ───────────────────────────────────────────

def test_summary_bar_markup(page):
    nodes = _tree(page)
    bar = _by_id(nodes, "fit-sumbar")
    assert bar["tag"] == "button" and bar["attrs"].get("type") == "button"
    assert "m-only" in bar["cls"] and "fit-sumbar" in bar["cls"]
    assert "hidden" in bar["attrs"], "the bar starts hidden until stats render"
    assert bar["attrs"].get("data-click") == "scrollToFitStats"
    idx = nodes.index(bar)
    hooks = [n["attrs"]["data-sumbar"] for n in nodes
             if "data-sumbar" in n["attrs"] and idx in _parents(nodes, n)]
    assert hooks == ["dps", "ehp", "cap"]
    labels = norm(bar["text"])
    assert labels.startswith("DPS") and "EHP" in labels and "Cap" in labels


def _parents(nodes, node):
    out, p = [], node["parent"]
    while p is not None:
        out.append(p)
        p = nodes[p]["parent"]
    return out


def test_summary_bar_updates_on_every_stats_write(script):
    rc = _fn(script, "recalcStats")
    ok, err = rc.split(".catch(", 1)
    for part, what in ((ok, "success"), (err, "error")):
        write = part.index("document.getElementById('stats-panel').innerHTML =")
        assert "_fitSumBarUpdate();" in part[write:], f"the {what} path must refresh the bar"


def test_summary_bar_update_reads_the_stats_hooks(script):
    up = _fn(script, "_fitSumBarUpdate")
    assert "#stats-panel [data-fit-sum]" in up
    assert "bar.hidden = !src;" in up, "no stats (or an error) hides the bar"
    for k in ("dps", "ehp", "cap"):
        assert f"'{k}'" in up
    assert "data-sum-' + k" in up and "data-sum-cap-ok" in up


def test_summary_bar_hides_while_overlay_or_modal_is_open(script):
    sync = _fn(script, "_fitSumBarSync")
    for needle in ("data-overlay", "charge-overlay", "info-modal", "charimport-modal", "import-modal"):
        assert needle in sync, f"_fitSumBarSync must check {needle}"
    assert "classList.toggle('is-covered', covered)" in sync
    watch = _fn(script, "_fitSumBarWatch")
    assert "_fitSumBarSync();" in watch and "new MutationObserver(_fitSumBarSync)" in watch
    assert "mo.observe(document.body, {childList: true})" in watch, (
        "the charge selector is appended to <body> while open")
    init = script[script.index("(function init()"):]
    assert "_fitSumBarWatch();" in init


def test_summary_bar_observes_the_overlay_and_every_modal(page, script):
    # _fitSumBarSync checks the modals, but it only runs when the observer
    # fires: a modal missing from the observed list would open over a bar
    # that never steps aside.
    watch = _fn(script, "_fitSumBarWatch")
    listed = re.search(r"\[([^\]]*)\]\.forEach\(", watch)
    assert listed, "the observed ids are one array literal"
    ids = re.findall(r"'([\w-]+)'", listed.group(1))
    nodes = _tree(page)
    for id_ in ("browser-panel", "info-modal", "charimport-modal", "import-modal"):
        assert id_ in ids, f"_fitSumBarWatch must observe #{id_}"
        _by_id(nodes, id_)
    # The modals open and close through style.display; the overlay through data-overlay.
    filt = re.search(r"attributeFilter: \[([^\]]*)\]", watch)
    assert filt and {"style", "data-overlay"} <= set(re.findall(r"'([\w-]+)'", filt.group(1)))


def test_summary_bar_tap_scrolls_to_stats(script):
    go = _fn(script, "scrollToFitStats")
    assert "document.getElementById('stats-panel')" in go and "scrollIntoView(" in go
    assert "prefers-reduced-motion" in go


def test_summary_bar_css():
    bar = _decls(".fit-sumbar")
    assert bar["position"] == "fixed" and bar["bottom"] == "0"
    assert bar["display"] == "flex"
    # 12px values and 11px keys: a capital's spool DPS, EHP and cap time fit
    # whole from 375px (at 13px they needed 390px), and no text in the bar
    # is under the 11px phone floor.
    assert bar["font-size"] == "12px"
    assert _decls(".fit-sumbar-k")["font-size"] == "11px"
    # Long values (spool DPS beside a capital's EHP) once pushed Cap past a
    # 320px screen. The gap is a floor under space-between, so ordinary
    # values sit where they did; each item truncates rather than overflow.
    assert bar["justify-content"] == "space-between" and bar["gap"] == "0.5rem"
    item = _decls(".fit-sumbar-item")
    assert item.get("min-width") == "0" and item.get("overflow") == "hidden", item
    assert item.get("text-overflow") == "ellipsis" and item.get("white-space") == "nowrap", item
    assert _decls(".fit-sumbar[hidden]")["display"] == "none !important"
    assert _decls(".fit-sumbar.is-covered")["display"] == "none !important"
    pad = _decls("body:has(#fit-sumbar:not([hidden]))")
    assert pad["padding-bottom"].startswith("calc(44px"), "the page clears the bar"
    assert _decls("#stats-panel")["scroll-margin-top"], "the jump clears the sticky nav"


def _stats(**over):
    s = defaultdict(int)
    s.update({"total_dps": 412.6, "total_ehp": 38250.0, "cap_drain_rate": 0,
              "cap_stable": False, "cap_stable_pct": 0, "cap_lasts_s": 0,
              "target_resist_profile": "uniform", "rah_suggested_phasing": {},
              "warnings": []})
    s.update(over)
    return s


def _summary(**over):
    html = render_page(fitting_mod, "partials/fitting_stats.html", stats=_stats(**over),
                       character_name=None, ship_name="Rifter", ship_type_id=587)
    nodes = _tree(html)
    hits = [n for n in nodes if "data-fit-sum" in n["attrs"]]
    assert len(hits) == 1
    return hits[0], nodes


@pytest.mark.parametrize("over, dps, ehp, cap, ok", [
    ({}, "413", "38,250", "100%", "1"),
    ({"cap_drain_rate": 12.5, "cap_stable": True, "cap_stable_pct": 47.3}, "413", "38,250", "47.3%", "1"),
    ({"cap_drain_rate": 30, "cap_lasts_s": 155}, "413", "38,250", "2m35s", "0"),
    ({"cap_drain_rate": 30, "cap_lasts_s": 42}, "413", "38,250", "42s", "0"),
    ({"total_dps": 0}, "0", "38,250", "100%", "1"),
    ({"spool_time_s": 90, "total_dps_max_spool": 980.2}, "413 / 980", "38,250", "100%", "1"),
])
def test_stats_partial_exposes_the_bar_values(over, dps, ehp, cap, ok):
    s, _ = _summary(**over)
    a = s["attrs"]
    assert (a["data-sum-dps"], a["data-sum-ehp"], a["data-sum-cap"], a["data-sum-cap-ok"]) == (
        dps, ehp, cap, ok)
    assert "fit-sum" in s["cls"], "the summary line carries its phone wrap hook"


def test_stats_summary_line_wraps_on_phones():
    s, nodes = _summary()
    idx = nodes.index(s)
    vals = [n for n in nodes if n["parent"] == idx and "fit-sum-vals" in n["cls"]]
    assert len(vals) == 1
    assert _decls(".fit-sum")["flex-wrap"] == "wrap"
    v = _decls(".fit-sum-vals")
    assert v["flex-wrap"] == "wrap" and v["row-gap"].endswith("!important")


def test_stats_unit_toggles_share_the_slot_control_hook():
    _, nodes = _summary()
    for id_ in ("fr-toggle", "def-toggle"):
        # .fit-ctl: 40px wide, 44px tall from the button floor. m-tap's 40px
        # !important height would shrink these buttons below that floor.
        assert _by_id(nodes, id_)["cls"] == ["fit-ctl"], id_


# ── 3. Slot rows ────────────────────────────────────────────────────

def test_slot_row_controls_carry_the_tap_hook(script):
    slots = _fn(script, "renderSlots")
    assert "fit-ctl" in _classes_in(_line(slots, 'data-click="toggleOnline"'), 'data-click="toggleOnline"')
    charge_btn = [ln for ln in slots.splitlines() if "<button" in ln and 'data-click="openChargeSelector"' in ln]
    assert len(charge_btn) == 1
    assert "fit-ctl" in _classes_in(charge_btn[0], 'data-click="openChargeSelector"')
    removes = [ln for ln in slots.splitlines() if 'data-click="removeItem"' in ln]
    assert len(removes) == 2, "module rows and drone rows"
    for ln in removes:
        assert "fit-ctl" in _classes_in(ln, 'data-click="removeItem"')
    qty = [ln for ln in slots.splitlines() if 'data-click="changeDroneQty"' in ln]
    assert len(qty) == 2
    for ln in qty:
        assert "fit-ctl" in _classes_in(ln, 'data-click="changeDroneQty"')


def test_state_dot_line_still_reads_one_title(script):
    # tests/test_fitting_blank_load_heat_toggle.py reads this line by regex.
    line = _line(_fn(script, "renderSlots"), 'data-click="toggleOnline"')
    assert "aria-label" in line and line.count("stateTitle") >= 2


def test_info_button_is_hidden_on_phones(script):
    slots = _fn(script, "renderSlots")
    infos = [ln for ln in slots.splitlines() if ">i</button>" in ln]
    assert len(infos) == 2, "module rows and drone rows"
    for ln in infos:
        assert _classes_in(ln, 'data-click="showModuleInfo"') == ["m-hide"]
    icons = [ln for ln in slots.splitlines() if "<img" in ln and 'data-click="showModuleInfo"' in ln]
    assert len(icons) == 2, "the icon still opens info"


def test_module_name_has_a_phone_copy_with_the_charge_line(script):
    slots = _fn(script, "renderSlots")
    desk = _line(slots, "escapeHtml(item.type_name) + chargeInfo")
    assert desk.strip().startswith("'<span class=\"m-hide\" style=\"flex:1;font-size:11px;color:var(--text);\">'"), (
        "the desktop name span keeps its inline style and only gains m-hide")
    phone = _line(slots, "fit-name-text")
    assert ("'<span class=\"m-only fit-name\"><span class=\"fit-name-text\">' + "
            "escapeHtml(item.type_name) + '</span>' + chargeLine + '</span>'") in phone
    charge = _line(slots, "chargeLine = '<span")
    assert 'class="fit-charge-name"' in charge and 'data-click="openChargeSelector"' in charge


def test_drone_name_truncates(script):
    slots = _fn(script, "renderSlots")
    drone = slots[slots.index("droneItems.forEach"):]
    assert "<span class=\"fit-dname\" style=\"flex:1;font-size:11px;color:var(--text);\">" in drone


def test_implant_and_booster_remove_carry_the_tap_hook(script):
    imp = _line(_fn(script, "renderImplantSlots"), 'data-click="removeImplant"')
    assert "fit-ctl" in _classes_in(imp, 'data-click="removeImplant"')
    boo = _line(_fn(script, "renderBoosterSlots"), 'data-click="removeBooster"')
    assert "fit-ctl" in _classes_in(boo, 'data-click="removeBooster"')


def _global_phone_button_floor():
    """min-height of the global phone rule for `button` (outside every task
    section): the floor .fit-ctl relies on for its 44px height."""
    with open(SITE_CSS, encoding="utf-8") as fh:
        css = re.sub(r"/\*.*?\*/", "", fh.read(), flags=re.S)
    m = re.search(r"@media \(max-width: 640px\) \{", css)
    floors = [b for sel, b in re.findall(r"([^{}]+)\{([^{}]*)\}", css[m.start():])
              if "button" in selectors(sel) and "min-height" in b]
    assert floors, "no phone rule gives every button a min-height"
    return norm(floors[0].split("min-height:", 1)[1].split(";", 1)[0])


def test_slot_control_css():
    ctl = _decls(".fit-ctl")
    # 40px wide (D11 A) so a high slot keeps "200mm AutoCannon II" whole at
    # 360px; 44px tall from the global button floor, which a class-level
    # min-height would outrank.
    assert ctl["min-width"] == "40px"
    assert "min-height" not in ctl and "height" not in ctl, "the global button rule gives 44px; a class would outrank it"
    assert _global_phone_button_floor() == "44px"
    assert ctl["display"] == "inline-flex"
    name = _decls(".fit-name")
    assert name["min-width"] == "0" and name["flex-direction"] == "column"
    for sel in (".fit-name-text", ".fit-charge-name", ".fit-dname"):
        d = _decls(sel)
        assert d["text-overflow"] == "ellipsis" and d["white-space"] == "nowrap", sel
    assert _decls(".fit-charge-name")["color"] == "var(--muted)"


# ── 4. Toolbars and forms ───────────────────────────────────────────

def test_header_toolbar_hook(page):
    nodes = _tree(page)
    bars = [n for n in nodes if "fit-toolbar" in n["cls"]]
    assert len(bars) == 1
    kids = [n for n in nodes if n["parent"] == nodes.index(bars[0])]
    assert sum("b-btn" in k["cls"] for k in kids) == 6
    btn = _decls(".fit-toolbar > .b-btn")
    assert btn["display"] == "inline-flex" and btn["align-items"] == "center"


def test_custom_sliders_become_a_2x2_grid(script):
    sync = _fn(script, "_syncCustomSliderUI")
    assert "wrap.classList.toggle('fit-dmg-on', isCustom);" in sync
    assert "wrap.style.display = isCustom ? 'inline-flex' : 'none';" in sync, (
        "desktop keeps its inline-flex row")
    grid = _decls("#dmg-custom-sliders.fit-dmg-on")
    assert grid["display"] == "grid !important"
    assert grid["grid-template-columns"] == "repeat(2, minmax(0, 1fr))"


# ── 5. EFT import ───────────────────────────────────────────────────

def test_import_modal_hooks(page):
    nodes = _tree(page)
    box = [n for n in nodes if "fit-import-box" in n["cls"]]
    assert len(box) == 1 and box[0]["parent"] == nodes.index(_by_id(nodes, "import-modal"))
    acts = [n for n in nodes if "fit-import-actions" in n["cls"]]
    assert len(acts) == 1
    clicks = [n["attrs"].get("data-click") for n in nodes if n["parent"] == nodes.index(acts[0])]
    assert clicks == ["hideImportModal", "doImportEFT"]


def test_import_modal_scrolls_with_reachable_buttons():
    assert _decls("#import-modal")["align-items"] == "flex-start !important"
    box = _decls(".fit-import-box")
    assert box["top"] == "0 !important"
    assert box["max-height"] and box["overflow-y"] == "auto"
    acts = _decls(".fit-import-actions")
    assert acts["position"] == "sticky" and acts["bottom"] == "0"


def test_import_textarea_is_16px_through_the_global_rule():
    css = open(SITE_CSS, encoding="utf-8").read()
    assert "input, select, textarea { font-size: 16px !important; }" in css


def test_modal_close_buttons_are_44px_and_clear_the_title(page):
    nodes = _tree(page)
    for modal, closer in (("info-modal", "hideInfoModal"), ("charimport-modal", "hideCharImportModal")):
        idx = nodes.index(_by_id(nodes, modal))
        x = [n for n in nodes if n["attrs"].get("data-click") == closer and idx in _parents(nodes, n)]
        assert len(x) == 1 and x[0]["cls"] == ["fit-modal-x"], modal
    x = _decls(".fit-modal-x")
    assert x["min-width"] == "44px"
    assert "min-height" not in x, "the global button rule gives 44px; don't cap it"
    assert x["top"] == "0 !important" and x["right"] == "0 !important"
    # A long module name ran under the info modal's ×.
    assert _decls("#info-modal-body > div:first-child")["padding-right"]


# ── Page width ──────────────────────────────────────────────────────

def test_grid_columns_never_grow_past_the_screen():
    # The nowrap phone name copy made the single 1fr track as wide as the
    # longest name (450px at a 360px viewport).
    assert _decls(".fit-grid > *")["min-width"] == "0"


# ── 44px floor ──────────────────────────────────────────────────────

_MTAP_BUTTON = re.compile(r"<button\b[^>]*\bclass=[\"'][^\"']*\bm-tap\b")


def test_no_button_carries_m_tap(page):
    # The shared .m-tap rule forces 40px !important, under the 44px the
    # global phone rule gives every button. The page includes its script, so
    # this covers the JS-string buttons too.
    assert _MTAP_BUTTON.search('<button id="x" class="a m-tap">'), "the check itself works"
    stats = render_page(fitting_mod, "partials/fitting_stats.html", stats=_stats(),
                        character_name=None, ship_name="Rifter", ship_type_id=587)
    for name, html in (("page", page), ("stats partial", stats)):
        assert not _MTAP_BUTTON.search(html), f"a <button> in the {name} carries m-tap"


def test_phone_block_sets_no_height_under_44px_except_on_div_rows():
    # A class or id selector outranks the global `button { min-height: 44px }`,
    # so a smaller height here would shrink a button. Only the browser's
    # tree and item rows and the charge selector's rows (divs, which the
    # floor doesn't reach) go to 40px.
    small = []
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", _phone()[0]):
        for prop, val in re.findall(r"(?:^|;)\s*((?:min-)?height)\s*:\s*([^;]+)", m.group(2)):
            n = re.fullmatch(r"(\d+(?:\.\d+)?)(px|rem)(?:\s*!important)?", val.strip())
            if n and float(n.group(1)) * (16 if n.group(2) == "rem" else 1) < 44:
                small.append((norm(m.group(1)), prop, val.strip()))
    assert small == [("#browser-panel .fit-brow", "min-height", "40px"),
                     ('.fit-charge-modal [data-click="selectCharge"]', "min-height", "40px")], small


# ── Section shape ───────────────────────────────────────────────────

def test_section_is_phone_only():
    _, after = _phone()
    assert not after.strip(), "desktop renders identically: no rule outside the phone block"


def test_charge_selector_has_its_phone_hook(script):
    show = _fn(script, "showChargeDropdown")
    assert "modal.className = 'fit-charge-modal';" in show
    assert 'data-click="clearCharge"' in show and 'data-click="selectCharge"' in show


def test_charge_selector_clear_and_rows_reach_the_tap_floor():
    clear = _decls('.fit-charge-modal [data-click="clearCharge"]')
    assert clear == {"min-width": "44px", "font-size": "12px !important"}, "no height: 44px from the button rule"
    assert _decls('.fit-charge-modal [data-click="selectCharge"]') == {"min-height": "40px"}
