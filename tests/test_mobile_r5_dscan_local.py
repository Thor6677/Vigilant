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
from tests._mobile import (SITE_CSS, VOID, assert_mrow, cells_rows, css_section, norm,
                           phone_block, render_page, request, row_keys, row_lead, rule_bodies,
                           selectors)

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
# Merging needs a login (POST /intel/{id}/merge sends a stranger to /), so
# the Add Scan button and its form render only for a logged-in viewer. Its
# bind() stays in the script, which skips an id that isn't on the page.
_LOGGED_IN_ONLY = {"add-scan-btn"}
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
    assert "if (el) el.addEventListener" in script.text, "bind() must skip a control that isn't rendered"
    for hook, _, fn in binds:
        # A duplicate id would bind only the first element.
        want = 0 if hook in _LOGGED_IN_ONLY else 1
        assert len(root.by_id(hook)) == want, f"#{hook} should appear {want} time(s) for a stranger"
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
    for hook, _, _ in binds:
        assert len(root.by_id(hook)) == 1, f"#{hook} should appear exactly once"
    controls = [n for hook, _, _ in binds for n in root.by_id(hook)]
    controls += [n for cls, _, _ in bind_all for n in root.by_class(cls)]
    assert len(controls) >= len(binds) + len(bind_all)
    for n in controls:
        bound = [k for k in n.attrs if _DEAD.match(f" {k}=")]
        assert not bound, f"<{n.tag} id={n.attrs.get('id')!r}> still carries {bound}"
    for _, _, fn in binds | bind_all:
        assert f'="{fn}"' not in html, f"something still dispatches {fn} through actions.js"


def _func_body(script, name):
    """The body of `function <name>(…) { … }` in a page script, found by
    counting braces from its opening one. The copy functions hold no brace
    inside a string or comment, which this would miscount."""
    m = re.search(rf"\bfunction {name}\([^)]*\)\s*\{{", script)
    assert m, f"no function {name} in the page script"
    depth, i = 1, m.end()
    while depth:
        depth += {"{": 1, "}": -1}.get(script[i], 0)
        i += 1
    return script[m.end():i - 1]


# The handlers behind each view's copy buttons.
_COPY_HANDLERS = {"dscan": ("copyShareUrl", "copyRawPaste", "copyShipClasses", "copyByType"),
                  "local": ("copyShareUrl", "copyRawPaste", "copyLocalComp")}


def test_copy_writes_inside_the_click_with_a_fallback():
    """iOS only lets a page write the clipboard during the tap, so writeText
    is called straight from the handler; execCommand('copy') covers a
    context with no clipboard API. Both are gesture-bound, so neither the
    handler, nor copyText, nor the fallback may put the write behind a
    timer or a fetch."""
    for view in _VIEWS:
        script = _page_script(_dom(_render_view(view, anon=True))).text
        copy, legacy = _func_body(script, "copyText"), _func_body(script, "legacyCopy")
        assert "navigator.clipboard.writeText(text)" in copy and "legacyCopy(text)" in copy
        assert "document.execCommand('copy')" in legacy
        # Control: the "Copied!" flash restores the label on a timer, so a
        # check over the whole script couldn't tell a deferred write apart.
        assert "setTimeout(" in _func_body(script, "flashCopied")
        bodies = {"copyText": copy, "legacyCopy": legacy}
        bodies.update((fn, _func_body(script, fn)) for fn in _COPY_HANDLERS[view])
        for fn, body in bodies.items():
            assert "setTimeout(" not in body, f"{view}: {fn} defers the copy behind a timer"
            assert "fetch(" not in body, f"{view}: {fn} fetches before the copy"
        for fn in _COPY_HANDLERS[view]:
            assert "copyText(" in bodies[fn], f"{view}: {fn} doesn't copy through copyText"


@pytest.mark.parametrize("view, anon", [(v, a) for v in sorted(_VIEWS) for a in (True, False)])
def test_share_url_is_a_hidden_input_read_only_by_copy_link(view, anon):
    """Copy Link copies #share-url's value. It used to be a text input moved
    off-screen: an unlabelled Tab stop that the phone audit flags as
    clipped. A hidden input still has the value and nothing else. The
    fallback copies through a textarea of its own, never this input."""
    root = _dom(_render_view(view, anon=anon))
    found = root.by_id("share-url")
    assert len(found) == 1
    inp = found[0]
    assert inp.tag == "input" and inp.attrs.get("type") == "hidden"
    assert inp.attrs["value"] == f"https://example.test/intel/{_SCAN_ID}"
    assert "style" not in inp.attrs, "a hidden input needs no off-screen positioning"
    script = _page_script(root).text
    assert "getElementById('share-url').value" in _func_body(script, "copyShareUrl")
    assert script.count("share-url") == 1, "only copyShareUrl reads the input"
    legacy = _func_body(script, "legacyCopy")
    assert "createElement('textarea')" in legacy and "share-url" not in legacy


@pytest.mark.parametrize("view, anon", [(v, a) for v in sorted(_VIEWS) for a in (True, False)])
def test_fold_out_toggles_have_the_tap_height_hook(view, anon):
    """D-Scan's Other on Scan and Local's All Pilots (the only way into the
    pilot list) are <summary> toggles. Each carries .intel-toggle for the
    phone rule's 40px; its inline display:flex and ▼ marker are unchanged."""
    root = _dom(_render_view(view, anon=anon))
    main = root.find_all(lambda n: n.tag == "main")
    assert len(main) == 1
    summaries = main[0].find_all(lambda n: n.tag == "summary")
    assert len(summaries) == 1
    toggle = summaries[0]
    assert toggle.classes == ["intel-toggle"] and toggle.parent.tag == "details"
    if view == "local":
        assert toggle.parent.attrs.get("id") == "pilot-details"
    assert toggle.text.startswith("Other on Scan" if view == "dscan" else "All Pilots")
    style = toggle.attrs["style"].replace(" ", "")
    assert "display:flex" in style and "min-height" not in style
    assert toggle.children[-1].tag == "span" and toggle.children[-1].text == "▼"


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


def _merge_forms(root):
    return root.find_all(lambda n: n.tag == "form" and n.attrs.get("action") == f"/intel/{_SCAN_ID}/merge")


def test_add_scan_is_hidden_from_logged_out_viewers():
    """A stranger's merge POST is redirected to / and the paste is lost, so
    neither the button nor the form renders for them."""
    root = _dom(_render_view("dscan", anon=True))
    assert not root.by_id("add-scan-btn") and not root.by_id("add-scan-section")
    assert not _merge_forms(root) and not root.by_class("intel-merge-row")
    assert "Add Scan" not in root.text and "Merge Additional D-Scan" not in root.text
    # Keep-for (extend) is out of scope and still renders for everyone.
    assert root.find_all(lambda n: n.tag == "form" and n.attrs.get("action", "").endswith("/extend"))


def test_add_scan_still_renders_for_logged_in_viewers():
    root = _dom(_render_view("dscan", anon=False))
    btn = root.by_id("add-scan-btn")
    assert len(btn) == 1 and btn[0].tag == "button" and btn[0].text == "Add Scan"
    section = root.by_id("add-scan-section")
    assert len(section) == 1 and "display:none" in section[0].attrs["style"].replace(" ", "")
    forms = _merge_forms(root)
    assert len(forms) == 1 and forms[0] in section[0].walk()
    assert forms[0].find_all(lambda n: n.tag == "textarea" and n.attrs.get("name") == "paste_text")
    assert len(root.by_class("intel-merge-row")) == 1


@pytest.mark.parametrize("view, anon, buttons", [
    ("dscan", True, ["Copy Link", "Raw Paste", "New Intel"]),
    ("dscan", False, ["Copy Link", "Add Scan", "Raw Paste", "New Intel"]),
    ("local", True, ["Copy Link", "Copy Summary", "Raw Paste", "New Intel"]),
    ("local", False, ["Copy Link", "Copy Summary", "Raw Paste", "New Intel"]),
])
def test_header_buttons_have_phone_hooks(view, anon, buttons):
    root = _dom(_render_view(view, anon=anon))
    actions = root.by_class("intel-actions")
    assert len(actions) == 1 and [c.text for c in actions[0].children] == buttons
    assert all("b-btn" in c.classes for c in actions[0].children)
    meta = root.by_class("intel-meta")
    assert len(meta) == 1 and "Expires in 2d 4h" in meta[0].text


def test_by_type_rows_have_phone_hooks():
    root = _dom(_render_view("dscan", anon=True))
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


def test_css_fold_out_toggles_are_40px_on_phones_only():
    """The Other on Scan / All Pilots summaries were 32.5px. Only their
    height changes: a display here would replace the inline display:flex
    (and with it how the summary and its ▼ marker lay out)."""
    body = _flat(rule_bodies(_phone(), ".intel-toggle"))
    assert "min-height: 40px" in body
    assert "display" not in body and "!important" not in body
    # Nothing outside this phone block reaches the toggles, so desktop keeps
    # its 32.5px row.
    with open(SITE_CSS, encoding="utf-8") as fh:
        css = re.sub(r"/\*.*?\*/", "", fh.read(), flags=re.S)
    assert css.count("intel-toggle") == _phone().count("intel-toggle") == 1


def test_css_section_leaves_share_url_alone():
    """#share-url is now a hidden input and needs no rule. R6 T3's image
    page shows an input with the same id, so a rule here would restyle it."""
    assert "share-url" not in _section("T2")


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
