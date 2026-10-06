"""v1.9.0: every phone m-row is at least 40px tall (mobile design §4.3, §4.6).

A collapsed m-row is one line of text plus the base rule's 0.6rem padding,
which measured 37–39px on many pages (wallet journal, skills queue, mining,
P&L, the order book, industry rows, dashboard Table/Compact). The floor is
one central declaration in the base `.m-row` phone rule of the R1 shared
layer, so every page gets it and no page section has to repeat it.

The line stays centred because the row's single grid track stretches to the
extra height (no align-content, so the grid default applies) and the base
rule's `align-items: center !important` centres the cells in that track.
These tests pin the declaration, keep it phone-only, and guard against a
later rule that would undo the floor or the centring."""
import glob
import os
import re

from tests._mobile import SITE_CSS, selectors

PHONE = "max-width: 640px"
_R2_FIRST = "/* ── R2 T1 · character overview ── */"
_ROOT = os.path.join(os.path.dirname(SITE_CSS), "..", "..")


def _raw() -> str:
    with open(SITE_CSS, encoding="utf-8") as fh:
        return fh.read()


def _strip(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def _media_bodies(css: str, query: str) -> list[str]:
    """Bodies of every @media block whose prelude contains `query`, brace-matched."""
    out = []
    for m in re.finditer(r"@media([^{]*)\{", css):
        if query not in m.group(1):
            continue
        depth, i = 1, m.end()
        while depth and i < len(css):
            depth += (css[i] == "{") - (css[i] == "}")
            i += 1
        out.append(css[m.end():i - 1])
    return out


def _outside_phone(css: str) -> str:
    """`css` with every phone @media block removed."""
    out, i = [], 0
    for m in re.finditer(r"@media([^{]*)\{", css):
        if m.start() < i or PHONE not in m.group(1):
            continue
        depth, j = 1, m.end()
        while depth and j < len(css):
            depth += (css[j] == "{") - (css[j] == "}")
            j += 1
        out.append(css[i:m.start()])
        i = j
    out.append(css[i:])
    return "".join(out)


def _rules(css: str):
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        yield selectors(m.group(1)), m.group(2)


def _decl(body: str, prop: str) -> str | None:
    m = re.search(rf"(?:^|[;\s]){re.escape(prop)}\s*:\s*([^;]+?)\s*(?:;|$)", body)
    return m.group(1) if m else None


def _base_rule() -> str:
    """The base `.m-row` rule: the only R1 phone-layer rule whose selector
    list is exactly `.m-row`."""
    raw = _raw()
    assert raw.count(_R2_FIRST) == 1
    r1 = _strip(raw[:raw.index(_R2_FIRST)])
    bodies = [body for block in _media_bodies(r1, PHONE)
              for sels, body in _rules(block) if sels == [".m-row"]]
    assert len(bodies) == 1, f"expected one R1 phone rule for .m-row, found {len(bodies)}"
    return bodies[0]


def _subject(selector: str) -> str:
    """The last compound selector (the element the rule styles): split at
    top-level descendant/child/sibling combinators, not inside :not()."""
    parts, depth, cur = [], 0, ""
    for ch in selector:
        depth += (ch == "(") - (ch == ")")
        if depth == 0 and (ch.isspace() or ch in ">+~"):
            if cur:
                parts.append(cur)
            cur = ""
        else:
            cur += ch
    if cur:
        parts.append(cur)
    return parts[-1] if parts else ""


_ROW = re.compile(r"\.m-row(?![-\w])")


def _row_rules():
    """(selector, body) for every phone rule, anywhere in site.css, that
    styles an m-row itself (its subject compound has .m-row and no
    pseudo-element), as opposed to a cell inside one or its ::after."""
    for block in _media_bodies(_strip(_raw()), PHONE):
        for sels, body in _rules(block):
            for sel in sels:
                subj = _subject(sel)
                if _ROW.search(subj) and "::" not in subj:
                    yield sel, body


# ── the floor ─────────────────────────────────────────────────────────

def test_base_m_row_rule_has_a_40px_floor():
    assert _decl(_base_rule(), "min-height") == "40px"


def test_the_floor_is_not_important_so_a_section_can_raise_it():
    """Pages that want 44px (Intel history) set their own, more specific
    floor; an !important here would beat them."""
    mh = _decl(_base_rule(), "min-height")
    assert mh is not None and "!important" not in mh, mh


def test_base_rule_still_centres_cells_without_align_content():
    """Centring comes from the stretched track plus align-items. An
    align-content here would stop the track stretching."""
    body = _base_rule()
    assert _decl(body, "display") == "grid !important"
    assert _decl(body, "align-items") == "center !important"
    for prop in ("align-content", "place-content", "grid-template-rows", "grid-auto-rows",
                 "height", "max-height"):
        assert _decl(body, prop) is None, prop


def test_the_floor_is_phone_only():
    """No m-row rule outside the phone blocks sets a height of any kind, so
    desktop rows are exactly as before."""
    for sels, body in _rules(_outside_phone(_strip(_raw()))):
        if any(_ROW.search(_subject(s)) for s in sels):
            for prop in ("min-height", "height", "max-height"):
                assert _decl(body, prop) is None, (sels, prop)


# ── nothing undoes it ──────────────────────────────────────────────────

def test_no_phone_rule_lowers_the_floor_or_caps_a_row():
    checked = 0
    for sel, body in _row_rules():
        checked += 1
        mh = _decl(body, "min-height")
        if mh is not None:
            m = re.fullmatch(r"(\d+(?:\.\d+)?)px(?:\s*!important)?", mh)
            assert m and float(m.group(1)) >= 40, f"{sel}: min-height {mh} is under the 40px floor"
        for prop in ("height", "max-height"):
            assert _decl(body, prop) is None, f"{sel}: {prop} would cap the row"
    # Not vacuous: the base rule, link rows, open states and page rows.
    assert checked >= 10, checked


def test_no_phone_rule_moves_a_rows_cells_off_centre():
    """align-content (or explicit row tracks) on a row would stop its single
    track stretching, leaving the line at the top of the 40px. A collapsed
    row keeps align-items centred; an open row may align its several lines
    to the top (Alliance Recent Changes does), since it is taller than the
    floor anyway."""
    for sel, body in _row_rules():
        for prop in ("align-content", "place-content", "grid-template-rows", "grid-auto-rows"):
            assert _decl(body, prop) is None, f"{sel}: {prop}"
        if not re.search(r"\.is-(?:open|expanded)\b", sel):
            ai = _decl(body, "align-items")
            assert ai is None or ai.startswith("center"), f"{sel}: align-items {ai}"


def test_cell_rules_are_not_mistaken_for_row_rules():
    """The subject check reads the last compound: a rule on a cell inside a
    row, or on a row's chevron, is not a row rule."""
    assert _ROW.search(_subject(".is-expanded > .m-row:not(.m-row--link)"))
    assert _ROW.search(_subject("#changes-list .m-row"))
    assert not _ROW.search(_subject('.ov-corp.m-row > [data-m="lead"]'))
    assert not _ROW.search(_subject(".m-row-x > span"))
    assert not _ROW.search(_subject(".m-rows"))
    assert "::" in _subject(".m-row:not(.m-row--link)::after")


_TAG = re.compile(r"<[a-z][a-z0-9]*\b[^<>]*>", re.I)
_CLASS_ROW = re.compile(r"""class\s*=\s*["'][^"']*(?<![-\w])m-row(?![-\w])""")
_STYLE_HEIGHT = re.compile(r"""style\s*=\s*["'][^"']*(?<![-\w])(?:min-|max-)?height\s*:""")


def test_no_markup_gives_an_m_row_an_inline_height():
    """An inline min-height/height/max-height on the row element would beat
    the floor (it isn't !important). Templates, route-built HTML and JS."""
    files = (glob.glob(os.path.join(_ROOT, "app", "templates", "**", "*.html"), recursive=True)
             + glob.glob(os.path.join(_ROOT, "app", "routes", "*.py"))
             + glob.glob(os.path.join(_ROOT, "static", "js", "*.js")))
    assert len(files) > 50, len(files)
    rows = 0
    for f in files:
        with open(f, encoding="utf-8") as fh:
            text = fh.read()
        for tag in _TAG.findall(text):
            if _CLASS_ROW.search(tag):
                rows += 1
                assert not _STYLE_HEIGHT.search(tag), f"{os.path.relpath(f, _ROOT)}: {tag[:160]}"
    assert rows >= 30, rows


# ── Dashboard Compact rows ─────────────────────────────────────────────
# Not m-rows (each is an <a class="dash-compact-row"> link, styled in
# dashboard.html with a 34px min-height that measured 37.8px on phones),
# so the R1 layer gives them the same 40px floor in a rule of their own.

def _r1_phone_bodies(selector: str) -> list[str]:
    raw = _raw()
    r1 = _strip(raw[:raw.index(_R2_FIRST)])
    return [body for block in _media_bodies(r1, PHONE)
            for sels, body in _rules(block) if selector in sels]


def test_dashboard_compact_rows_have_a_40px_phone_floor():
    bodies = _r1_phone_bodies("a.dash-compact-row")
    assert len(bodies) == 1, bodies
    assert _decl(bodies[0], "min-height") == "40px"


def test_the_compact_floor_is_phone_only():
    """The 641-1000px tiers and desktop keep the template's 34px."""
    assert "dash-compact-row" not in _outside_phone(_strip(_raw()))


def test_the_compact_floor_outranks_the_template_rule():
    """dashboard.html's own rule loads after site.css, so the floor wins by
    specificity alone: the template's selector must stay a bare class and
    the row an <a>."""
    with open(os.path.join(_ROOT, "app", "templates", "dashboard.html"), encoding="utf-8") as fh:
        page = fh.read()
    styles = _strip("\n".join(re.findall(r"<style\b[^>]*>(.*?)</style>", page, flags=re.S)))
    sels = [s for m in re.finditer(r"([^{}]+)\{[^{}]*min-height[^{}]*\}", styles)
            for s in selectors(m.group(1)) if "dash-compact-row" in s]
    assert sels == [".dash-compact-row"], sels
    with open(os.path.join(_ROOT, "app", "templates", "partials", "dashboard_compact_row.html"),
              encoding="utf-8") as fh:
        row = fh.read()
    assert re.search(r'<a\b[^>]*class="dash-compact-row"', row)
