"""Mobile R5 T2: the D-Scan / Local paste page and the public scan views at
phone width, plus ISS-120.

`/intel/dscan` (intel.html) is the paste form plus, for a logged-in user,
the History table. On phones each history row is a link row (D4 A): the
type badge leads, the label is key 1 and the count key 2. The row keeps
its own goTo and never toggles.

`/intel/{scan_id}` is public and renders intel_dscan.html or
intel_local.html. base.html loads actions.js only for a logged-in session,
so a data-click/data-input there was dead for the people a scan link is
shared with (ISS-120). Each control now has an id or a js- hook and is
bound by the template's own nonce'd script, and none carries a data-*
binding, so it works without actions.js and never fires twice with it.
The tests/test_route_auth_gating.py sweep can't catch this page: it skips
parametrised paths, so the views are rendered directly here.

On phones the Local view's pilot rows are two lines (D5 A), its corp rows
put the alliance on a muted, truncating second line (D6 A), and the header
buttons wrap. Contexts follow the shapes app/routes/dscan.py builds; every
name and id is invented."""
import functools
import re
import types
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

from app.routes import dscan as dscan_mod
from tests._mobile import (VOID, assert_mrow, cells_rows, css_section, norm, phone_block,
                           render_page, request, row_keys, row_lead, rule_bodies, selectors)

_section = functools.partial(css_section, release="R5")
_NS = types.SimpleNamespace

_SCAN_ID = "samplescan01"
# Every event actions.js dispatches from a data-* attribute. Not its
# data-on-error="hide" image shortcut: that isn't a control (see the report
# for ISS-120), so it stays out of this check.
_DEAD = re.compile(r'\sdata-(?:click|change|input|submit|keydown|mousedown|focus)=')
# The script tag itself: base.html also names actions.js in comments.
_ACTIONS_SCRIPT = re.compile(r'<script\b[^>]*\bsrc="[^"]*/actions\.js\b')
_BIND = re.compile(r"""\bbind\('([\w-]+)', '(\w+)', (\w+)\)""")
_BIND_ALL = re.compile(r"""\bbindAll\('([\w-]+)', '(\w+)', (\w+)\)""")

# (hook, event, handler) for every control in brief item 2, plus the two
# the brief didn't name (toggleAddScan, copyRawPaste) that were just as dead.
_DSCAN_BINDS = {
    ("copy-url-btn", "click", "copyShareUrl"),
    ("add-scan-btn", "click", "toggleAddScan"),
    ("raw-paste-btn", "click", "toggleRawPaste"),
    ("copy-raw-btn", "click", "copyRawPaste"),
    ("copy-classes-btn", "click", "copyShipClasses"),
    ("copy-types-btn", "click", "copyByType"),
}
_LOCAL_BINDS = {
    ("copy-url-btn", "click", "copyShareUrl"),
    ("copy-comp-btn", "click", "copyLocalComp"),
    ("raw-paste-btn", "click", "toggleRawPaste"),
    ("copy-raw-btn", "click", "copyRawPaste"),
    ("intel-search", "input", "filterIntel"),
}
_LOCAL_BIND_ALL = {
    ("js-filter-corp", "click", "filterByCorp"),
    ("js-filter-alliance", "click", "filterByAlliance"),
}
_VIEWS = {"dscan": ("intel_dscan.html", _DSCAN_BINDS, set()),
          "local": ("intel_local.html", _LOCAL_BINDS, _LOCAL_BIND_ALL)}


# ── a small DOM ───────────────────────────────────────────────────────

class _Node:
    def __init__(self, tag, attrs, parent=None):
        self.tag, self.attrs, self.parent = tag, attrs, parent
        self.children, self.parts = [], []

    @property
    def classes(self):
        return self.attrs.get("class", "").split()

    @property
    def text(self):
        return norm("".join(p if isinstance(p, str) else p.text for p in self.parts))

    def walk(self):
        for c in self.children:
            yield c
            yield from c.walk()

    def find_all(self, pred):
        return [n for n in self.walk() if pred(n)]

    def by_class(self, cls):
        return self.find_all(lambda n: cls in n.classes)

    def by_id(self, id_):
        return self.find_all(lambda n: n.attrs.get("id") == id_)


class _Tree(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("#root", {})
        self.cur = self.root

    def handle_starttag(self, tag, attrs):
        n = _Node(tag, {k: (v if v is not None else "") for k, v in attrs}, self.cur)
        self.cur.children.append(n)
        self.cur.parts.append(n)
        if tag not in VOID:
            self.cur = n

    def handle_endtag(self, tag):
        n = self.cur
        while n is not self.root and n.tag != tag:
            n = n.parent
        if n is not self.root:
            self.cur = n.parent

    def handle_data(self, data):
        self.cur.parts.append(data)


def _dom(html):
    t = _Tree()
    t.feed(html)
    t.close()
    return t.root


def _page_script(root):
    """The view's own inline script: the one that defines bind()."""
    found = [s for s in root.find_all(lambda n: n.tag == "script" and "src" not in n.attrs)
             if "function bind(" in s.text]
    assert len(found) == 1, f"expected one page script defining bind(), found {len(found)}"
    return found[0]


# ── contexts ──────────────────────────────────────────────────────────

def _pilot(i, name, corp, alliance):
    return {"name": name, "character_id": 90000100 + i, "corporation_id": 98000100 + i,
            "corporation_name": corp, "alliance_id": (99000100 + i) if alliance else None,
            "alliance_name": alliance}


_PILOTS = [
    _pilot(1, "Sample Pilot Alpha", "Sample Corp One", "Sample Alliance"),
    _pilot(2, "Sample Pilot Bravo", "Sample Corp One", "Sample Alliance"),
    _pilot(3, "Sample Pilot Charlie", "Sample Corp Two", None),
]


def _local_ctx():
    summary = {
        "type": "local", "total": 3, "resolved": 3, "unresolved_count": 0, "unresolved": [],
        "characters": _PILOTS,
        "by_corp": {"Sample Corp One": 2, "Sample Corp Two": 1},
        "by_alliance": {"Sample Alliance": 2},
        "corp_to_alliance": {"Sample Corp One": "Sample Alliance", "Sample Corp Two": None},
    }
    return {"dscan": _NS(label="Sample staging", paste_data="Sample Pilot Alpha\nSample Pilot Bravo"),
            "items": _PILOTS, "non_ship_items": [], "summary": summary}


def _dscan_ctx():
    items = [
        {"type_id": 601, "type_name": "Sample Hull A", "hull_name": "Sample Hull A",
         "category": "Frigate", "distance_str": "1.2 AU"},
        {"type_id": 601, "type_name": "Sample Hull A", "hull_name": "Sample Hull A",
         "category": "Frigate", "distance_str": "1.4 AU"},
        {"type_id": 602, "type_name": "Sample Hull B", "hull_name": "Sample Hull B",
         "category": "Cruiser", "distance_str": "0.8 AU"},
        {"type_id": 11, "type_name": "Sample Planet", "category": "Celestial",
         "distance_str": "4.1 AU"},
    ]
    summary = dscan_mod.build_dscan_summary(items)
    non_ship = [i for i in items if i["category"] not in set(dscan_mod.SHIP_GROUP_CATEGORIES.values())]
    return {"dscan": _NS(label="Sample gate", paste_data="raw paste"),
            "items": items, "non_ship_items": non_ship, "summary": summary}


def _render_view(view, *, anon):
    """A scan view through dscan.py's env, the way intel_view renders it.
    The views call request.url_for, which the shared stand-in lacks."""
    import app.main  # noqa: F401 — populates the env's globals and filters
    template = _VIEWS[view][0]
    req = request(f"/intel/{_SCAN_ID}", session=None) if anon else request(f"/intel/{_SCAN_ID}")
    req.url_for = lambda name, **kw: f"https://example.test/intel/{kw['scan_id']}"
    ctx = _dscan_ctx() if view == "dscan" else _local_ctx()
    return dscan_mod.templates.env.get_template(template).render(
        request=req, scan_id=_SCAN_ID, expires_str="2d 4h",
        expiry_options=dscan_mod.EXPIRY_OPTIONS, **ctx)


# ── 1. History: link rows (D4 A) ──────────────────────────────────────

_HISTORY = [
    {"id": "histaaa111", "label": "Sample gate camp", "scan_type": "dscan",
     "detail": "12 ships / 48 total", "expires_in": "3d"},
    {"id": "histbbb222", "label": None, "scan_type": "local",
     "detail": "87 pilots", "expires_in": "5h"},
    {"id": "histccc333", "label": "Sample hole", "scan_type": "dscan",
     "detail": "0 ships / 4 total", "expires_in": "12m"},
]


def test_history_rows_are_link_rows():
    html = render_page(dscan_mod, "intel.html", "/intel/dscan", history=_HISTORY)
    assert len(assert_mrow(html, 3)) == 3
    assert "toggleMRow" not in html
    for row, scan in zip(cells_rows(html), _HISTORY):
        assert "m-row--link" in row["attrs"]["class"].split()
        assert row["attrs"]["data-click"] == "goTo"
        assert row["attrs"]["data-href"] == f"/intel/{scan['id']}"
        assert [c["text"] for c in row_lead(row)] == ["D-Scan" if scan["scan_type"] == "dscan" else "Local"]
        assert [c["text"] for c in row_keys(row)] == [scan["label"] or "—", scan["detail"]]
    root = _dom(html)
    table = root.find_all(lambda n: n.tag == "table")
    assert len(table) == 1 and "m-table" in table[0].classes
    thead = table[0].find_all(lambda n: n.tag == "thead")
    assert len(thead) == 1 and "m-head" in thead[0].classes
    # Hook for the section's phone rules; the key 2 count stays muted.
    assert "intel-history" in table[0].classes
    for row in cells_rows(html):
        assert "color:var(--muted)" in row_keys(row)[1]["attrs"].get("style", "")


def test_history_is_absent_for_anonymous_visitors():
    """The route builds no history without a session, so a stranger sees the
    paste form and nothing else: no rows and no actions.js binding."""
    import app.main as main
    r = TestClient(main.app, follow_redirects=False).get("/intel/dscan")
    assert r.status_code == 200
    assert "Paste D-Scan or Local" in r.text and "No recent scans." in r.text
    assert "m-row" not in r.text and "goTo" not in r.text
    assert not _DEAD.search(r.text)


def test_paste_textarea_is_full_width():
    html = render_page(dscan_mod, "intel.html", "/intel/dscan", history=[])
    areas = _dom(html).find_all(lambda n: n.tag == "textarea" and n.attrs.get("name") == "paste_text")
    assert len(areas) == 1 and "width:100%" in areas[0].attrs["style"].replace(" ", "")


# ── 2. ISS-120: the public views bind their own controls ──────────────

@pytest.mark.parametrize("view", sorted(_VIEWS))
def test_anonymous_view_binds_every_control_itself(view):
    html = _render_view(view, anon=True)
    assert not _ACTIONS_SCRIPT.search(html), "the premise: no actions.js for a stranger"
    hit = _DEAD.search(html)
    assert not hit, f"anonymous {view} view still relies on {hit.group(0).strip()}"
    assert "toggleMRow" not in html

    root = _dom(html)
    # No tap-to-open on a page a stranger sees.
    assert not root.by_class("m-row")
    script = _page_script(root)
    assert script.attrs.get("nonce") == "test-nonce"
    _, binds, bind_all = _VIEWS[view]
    assert set(_BIND.findall(script.text)) == binds
    assert set(_BIND_ALL.findall(script.text)) == bind_all
    assert "addEventListener(type, fn)" in script.text
    for hook, _, fn in binds:
        # A duplicate id would bind only the first element.
        assert len(root.by_id(hook)) == 1, f"#{hook} should appear exactly once"
        assert re.search(rf"\bfunction {fn}\(", script.text), f"{fn} isn't defined in the page script"
    for cls, _, fn in bind_all:
        assert root.by_class(cls), f"no .{cls} element to bind"
        assert re.search(rf"\bfunction {fn}\(", script.text), f"{fn} isn't defined in the page script"


@pytest.mark.parametrize("view", sorted(_VIEWS))
def test_logged_in_view_binds_nothing_twice(view):
    """Logged in, actions.js is loaded. Control: it is, so the anonymous
    test passes because of the session. None of the page's controls carries
    a data-* binding, so actions.js can't fire one as well."""
    html = _render_view(view, anon=False)
    assert _ACTIONS_SCRIPT.search(html)
    root = _dom(html)
    _, binds, bind_all = _VIEWS[view]
    controls = [n for hook, _, _ in binds for n in root.by_id(hook)]
    controls += [n for cls, _, _ in bind_all for n in root.by_class(cls)]
    assert len(controls) >= len(binds) + len(bind_all)
    for n in controls:
        bound = [k for k in n.attrs if _DEAD.match(f" {k}=")]
        assert not bound, f"<{n.tag} id={n.attrs.get('id')!r}> still carries {bound}"
    for _, _, fn in binds | bind_all:
        assert f'="{fn}"' not in html, f"something still dispatches {fn} through actions.js"


def test_copy_writes_inside_the_click_with_a_fallback():
    """iOS only lets a page write the clipboard during the tap, so writeText
    is called straight from the handler; execCommand('copy') covers a
    context with no clipboard API."""
    for view in _VIEWS:
        script = _page_script(_dom(_render_view(view, anon=True))).text
        assert "navigator.clipboard.writeText(text)" in script
        assert "document.execCommand('copy')" in script
        # Never deferred behind a timer or a fetch before the write.
        assert "fetch(" not in script


# ── 3–5. Local: filter, pilot lines, corp rows ───────────────────────

def test_local_pilot_rows_are_two_lines_on_phones():
    root = _dom(_render_view("local", anon=True))
    details = root.by_id("pilot-details")
    assert len(details) == 1 and details[0].tag == "details"
    rows = details[0].by_class("intel-pilot-row")
    assert len(rows) == 3
    head = [n for n in details[0].by_class("b-table-row") if "intel-pilot-row" not in n.classes]
    assert len(head) == 1 and "m-head" in head[0].classes

    for row, p in zip(rows, _PILOTS):
        assert "m-row" not in row.classes and not _DEAD.search(" ".join(f" {k}=" for k in row.attrs))
        # The search filter reads these; they must survive.
        assert row.attrs["data-name"] == p["name"].lower()
        assert row.attrs["data-corp"] == p["corporation_name"].lower()
        name, corp, alliance, org = row.children
        # Line 1: the portrait, then the name.
        assert "intel-pilot-name" in name.classes and "m-hide" not in name.classes
        assert [c.tag for c in name.children] == ["img", "span"] and name.text == p["name"]
        # Desktop's corp and alliance cells are unchanged and phone-hidden.
        assert "m-hide" in corp.classes and corp.text == p["corporation_name"]
        assert "m-hide" in alliance.classes and alliance.text == (p["alliance_name"] or "—")
        # Line 2: "corporation · alliance", the alliance left out when there is none.
        assert {"m-only", "intel-pilot-org"} <= set(org.classes)
        want = f"{p['corporation_name']} · {p['alliance_name']}" if p["alliance_name"] else p["corporation_name"]
        assert org.text == want
    assert "·" not in rows[2].children[3].text and "—" not in rows[2].children[3].text


def test_local_corp_rows_put_the_alliance_on_a_hooked_second_line():
    root = _dom(_render_view("local", anon=True))
    rows = root.by_class("intel-corp-row")
    assert len(rows) == 2
    one, two = rows
    for row in rows:
        assert "js-filter-corp" in row.classes and row.attrs["data-corp-name"]
    assert [c.classes[-1] for c in one.children] == ["intel-corp-count", "intel-corp-name", "intel-corp-alliance"]
    assert [c.text for c in one.children] == ["2", "Sample Corp One", "Sample Alliance"]
    assert [c.classes[-1] for c in two.children] == ["intel-corp-count", "intel-corp-name"]
    for row in root.by_class("intel-alliance-row"):
        assert "js-filter-alliance" in row.classes and row.attrs["data-alliance-name"]


def test_header_buttons_and_merge_row_have_phone_hooks():
    for view in _VIEWS:
        root = _dom(_render_view(view, anon=True))
        actions = root.by_class("intel-actions")
        assert len(actions) == 1 and len(actions[0].children) == 4
        assert all("b-btn" in c.classes for c in actions[0].children)
        meta = root.by_class("intel-meta")
        assert len(meta) == 1 and "Expires in 2d 4h" in meta[0].text
    root = _dom(_render_view("dscan", anon=True))
    assert len(root.by_class("intel-merge-row")) == 1
    groups, ships = root.by_class("dscan-group-row"), root.by_class("dscan-ship-row")
    assert [g.text for g in groups] == ["1 Cruisers", "2 Frigates"]
    assert len(ships) == 2


# ── CSS ───────────────────────────────────────────────────────────────

def _phone():
    body, after = phone_block(_section("T2"))
    assert not after.strip(), "desktop must render as before: no rule outside the phone block"
    return body


def _flat(s):
    return re.sub(r"\s+", " ", s).strip()


def test_css_header_buttons_wrap():
    body = _flat(rule_bodies(_phone(), ".intel-actions"))
    assert "flex-wrap: wrap" in body
    btn = _flat(rule_bodies(_phone(), ".intel-actions > .b-btn"))
    # Beats the inline flex:none that kept all four on one line.
    assert re.search(r"flex: [^;]*!important", btn)
    # The meta line under the title wraps between phrases, never inside one.
    assert "flex-wrap: wrap" in _flat(rule_bodies(_phone(), ".intel-meta"))
    assert "white-space: nowrap" in _flat(rule_bodies(_phone(), ".intel-meta > span"))


def test_css_pilot_rows_wrap_to_a_truncating_second_line():
    phone = _phone()
    assert "flex-wrap: wrap" in _flat(rule_bodies(phone, ".intel-pilot-row"))
    org = _flat(rule_bodies(phone, ".intel-pilot-row > .intel-pilot-org"))
    for decl in ("flex: 0 0 100%", "min-width: 0", "overflow: hidden",
                 "text-overflow: ellipsis", "white-space: nowrap", "color: var(--muted)"):
        assert decl in org, f"{decl!r} missing from the pilot org line"


def test_css_corp_alliance_line_truncates():
    phone = _phone()
    row = _flat(rule_bodies(phone, ".intel-corp-row"))
    assert "display: grid" in row and "grid-template-columns: minmax(0, 1fr) auto" in row
    assert "min-height: 40px" in row
    ally = _flat(rule_bodies(phone, ".intel-corp-row > .intel-corp-alliance"))
    for decl in ("grid-column: 1 / -1", "min-width: 0", "overflow: hidden",
                 "text-overflow: ellipsis", "white-space: nowrap"):
        assert decl in ally, f"{decl!r} missing from the corp alliance line"
    assert "min-height: 40px" in _flat(rule_bodies(phone, ".intel-alliance-row"))


def test_css_never_forces_display_on_filtered_rows():
    """applyFilter hides pilot, corp and alliance rows with an inline
    display:none. A display with !important on them would beat it on phones
    and the search would silently stop filtering (brief item 3)."""
    phone = _phone()
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", phone):
        for sel in selectors(m.group(1)):
            subject = sel.split()[-1] if sel.split() else sel
            if re.search(r"\.intel-(?:pilot|corp|alliance)-row\b", subject):
                assert not re.search(r"display\s*:[^;]*!important", m.group(2)), (
                    f"{sel} forces display, which beats applyFilter's inline display:none")
