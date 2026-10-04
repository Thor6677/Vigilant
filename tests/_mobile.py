"""Shared helpers for the mobile phone-layer tests (mobile design §4.3).

Helpers, one line each:
  assert_mrow(html, min_rows=1)  check every .m-row against the contract (below); returns the rows
  mrows(html)                    every .m-row's tag, attrs and direct children's attrs and tags
  request(path, session=...)     a stand-in request; logged-in by default, session=None (or {}) is anonymous
  render_page(module, template, path, session=..., **ctx)
                                 a full page through the route module's own templates.env
  source(name)                   a template's source text, from app/templates
  norm(s)                        collapse runs of whitespace and strip
  cells_rows(html)               every .m-row with its direct-child cells: attrs, text, element children
  row_keys(row)                  a cells_rows row's data-m="key" cells, in DOM order
  row_lead(row)                  a cells_rows row's data-m="lead" cells
  row_labelled(row)              a cells_rows row's labelled cells, as {label: cell}
  assert_single_value_child(row) each labelled cell has at most one shown element child
  SITE_CSS                       path to static/css/site.css
  clamps(html)                   .m-clamp lists, .m-showall buttons, .m-unclamp and .m-clamp-wrap counts
  Styled                         HTMLParser collecting every start tag's name, classes and inline style
  VOID                           the void HTML elements (no end tag), for a test's own HTMLParser
  css_section(task, css=None)    an R2 task's site.css section ("T1"…"T6", "P"), comments stripped
  phone_block(section)           a css_section's phone @media body (brace-matched) and what follows it
  selectors(prelude)             split a CSS selector list on its top-level commas (not inside :not())
  rule_bodies(css, selector)     joined bodies of every rule whose selector list contains `selector`

`assert_mrow(html)` parses rendered HTML and checks every element with class
`m-row`:
  * 1–2 direct children with data-m="key"
  * at most 1 direct child with data-m="lead"; an <img> lead carries
    width and height attributes (matching any inline px size). The phone CSS
    forces width:auto !important on row children, so without them an
    unloaded or broken image collapses to ~2px and the row jumps when it
    arrives.
  * every data-m-label is non-empty
  * a non-link row is toggled by toggleMRow or by its own toggleExpanded;
    a link row (m-row--link) is never toggled by toggleMRow
It returns the parsed rows so a test can make page-specific checks too."""
import os
import re
import types
from html.parser import HTMLParser

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "source", "track", "wbr"}


class _Collector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []   # (tag, row_index_or_None)
        self.rows = []    # {"tag", "attrs", "children": [attrs...], "child_tags": [tag...]}

    def handle_starttag(self, tag, attrs):
        a = {k: (v if v is not None else "") for k, v in attrs}
        if self.stack and self.stack[-1][1] is not None:
            row = self.rows[self.stack[-1][1]]
            row["children"].append(a)
            row["child_tags"].append(tag)
        idx = None
        if "m-row" in a.get("class", "").split():
            self.rows.append({"tag": tag, "attrs": a, "children": [], "child_tags": []})
            idx = len(self.rows) - 1
        if tag not in VOID:
            self.stack.append((tag, idx))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                return


def _assert_img_size(where: str, img: dict) -> None:
    for dim in ("width", "height"):
        val = img.get(dim, "").strip()
        assert val.isdigit() and int(val) > 0, (
            f"{where}: an <img data-m=lead> needs a positive {dim} attribute, got {img.get(dim)!r}")
        inline = re.search(rf"(?:^|;)\s*{dim}\s*:\s*(\d+)px", img.get("style", ""))
        assert not inline or inline.group(1) == val, (
            f"{where}: <img data-m=lead> {dim}={val!r} doesn't match its inline {dim}:{inline.group(1)}px")


def mrows(html: str) -> list[dict]:
    c = _Collector()
    c.feed(html)
    c.close()
    return c.rows


def assert_mrow(html: str, min_rows: int = 1) -> list[dict]:
    rows = mrows(html)
    assert len(rows) >= min_rows, f"expected at least {min_rows} .m-row, found {len(rows)}"
    for n, r in enumerate(rows):
        where = f"m-row #{n} <{r['tag']} class=\"{r['attrs'].get('class', '')}\">"
        kids = r["children"]
        keys = [k for k in kids if k.get("data-m") == "key"]
        leads = [k for k in kids if k.get("data-m") == "lead"]
        assert 1 <= len(keys) <= 2, f"{where}: needs 1–2 data-m=key children, has {len(keys)}"
        assert len(leads) <= 1, f"{where}: at most 1 data-m=lead child, has {len(leads)}"
        for tag, k in zip(r["child_tags"], kids):
            if tag == "img" and k.get("data-m") == "lead":
                _assert_img_size(where, k)
        for k in kids:
            assert not ("data-m" in k and "data-m-label" in k), (
                f"{where}: a cell can't be both data-m and data-m-label")
            if "data-m-label" in k:
                assert k["data-m-label"].strip(), f"{where}: empty data-m-label"
        is_link = "m-row--link" in r["attrs"].get("class", "").split()
        click = r["attrs"].get("data-click", "")
        if is_link:
            assert click != "toggleMRow", f"{where}: link rows must not toggle"
        else:
            assert click in ("toggleMRow", "toggleExpanded"), (
                f"{where}: expandable rows need data-click=toggleMRow (or their own toggleExpanded), got {click!r}")
    return rows


# ── Page rendering ────────────────────────────────────────────────────

_ROOT = os.path.join(os.path.dirname(__file__), "..")
_TEMPLATES = os.path.join(_ROOT, "app", "templates")
_NS = types.SimpleNamespace

SITE_CSS = os.path.join(_ROOT, "static", "css", "site.css")

# The session R1's page tests rendered with: a logged-in, non-admin user
# with an active character. base.html loads actions.js (and so every
# data-click binding works) only when user_id is set.
_LOGGED_IN = {"user_id": 1, "is_admin": False,
              "active_character_id": 90000001, "csrf_token": "t"}
_DEFAULT = object()


def request(path="/", session=_DEFAULT):
    """A stand-in for the Starlette request a template reads: url (path,
    scheme, netloc, query), query_params, headers, cookies, state.csp_nonce
    and session. Leave `session` out for the logged-in
    session above (a fresh copy each call); pass None or {} for an
    anonymous visitor (empty session, no user_id); or pass your own dict."""
    if session is _DEFAULT:
        session = dict(_LOGGED_IN)
    elif not session:
        session = {}
    return _NS(url=_NS(path=path, scheme="https", netloc="example.test", query=""),
               query_params={}, headers={}, cookies={},
               state=_NS(csp_nonce="test-nonce"), session=session)


def render_page(module, template, path="/", *, session=_DEFAULT, **ctx):
    """Render `template` through the route module's own `templates.env`: a
    page that extends base.html renders in full; a partial (no extends)
    renders on its own, so use this for partials too. app.main is imported
    first so every env carries the globals and filters the real routes
    register. `session` is passed to request(); the rest is the template
    context."""
    import app.main  # noqa: F401 — populates every router's templates.env.globals
    return module.templates.env.get_template(template).render(
        request=request(path, session), **ctx)


def source(name):
    """The source text of app/templates/<name>."""
    with open(os.path.join(_TEMPLATES, name), encoding="utf-8") as fh:
        return fh.read()


def norm(s):
    return re.sub(r"\s+", " ", s).strip()


# ── m-row cells ───────────────────────────────────────────────────────

class _Cells(HTMLParser):
    """For every .m-row: its attrs, and each direct child's attrs, text and
    element children (attrs of the child's own direct children).

    Limits, all acceptable for the hand-written templates it reads:
    - No implied end tags. An unclosed <td>, <li> or <p> stays open, so the
      next sibling is read as its child rather than as another cell.
    - An end tag closes the nearest open element with that name, so a stray
      end tag can close a row early. Cells after it are dropped, which could
      hide a third key from assert_mrow.
    - `row_labelled` keys cells by label, so two cells with the same label
      merge into the last one."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []   # [tag, kind, ref]; kind: "row" | "cell" | "in" | None
        self.rows = []

    def handle_starttag(self, tag, attrs):
        a = {k: (v if v is not None else "") for k, v in attrs}
        parent = self.stack[-1] if self.stack else None
        entry = [tag, None, None]
        if parent and parent[1] == "row":
            cell = {"tag": tag, "attrs": a, "text": "", "kids": []}
            self.rows[parent[2]]["cells"].append(cell)
            entry = [tag, "cell", cell]
        elif parent and parent[1] in ("cell", "in"):
            if parent[1] == "cell":
                parent[2]["kids"].append(a)
            entry = [tag, "in", parent[2]]
        if "m-row" in a.get("class", "").split():
            self.rows.append({"tag": tag, "attrs": a, "cells": []})
            entry = [tag, "row", len(self.rows) - 1]
        if tag not in VOID:
            self.stack.append(entry)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        for e in reversed(self.stack):
            if e[1] in ("cell", "in"):
                e[2]["text"] += data
                return
            if e[1] == "row":
                return


def cells_rows(html):
    """Every .m-row as {"tag", "attrs", "cells"}; each cell is {"tag",
    "attrs", "text" (normalised), "kids" (its element children's attrs)}.
    See _Cells for the parser's limits."""
    p = _Cells()
    p.feed(html)
    p.close()
    for r in p.rows:
        for c in r["cells"]:
            c["text"] = norm(c["text"])
    return p.rows


def row_keys(row):
    return [c for c in row["cells"] if c["attrs"].get("data-m") == "key"]


def row_lead(row):
    return [c for c in row["cells"] if c["attrs"].get("data-m") == "lead"]


def row_labelled(row):
    return {c["attrs"]["data-m-label"]: c for c in row["cells"] if "data-m-label" in c["attrs"]}


def assert_single_value_child(row):
    """An open row lays a labelled cell out as label · value, with the value
    pieces grouped on the right (justify-content:flex-end; flex-wrap:wrap),
    so several element children would still render. This helper is stricter
    than the CSS on purpose: one wrapper per value keeps each value laid out
    as a unit, the way its desktop cell is. m-hide children don't count."""
    for label, c in row_labelled(row).items():
        shown = [k for k in c["kids"] if "m-hide" not in k.get("class", "").split()]
        assert len(shown) <= 1, f"labelled cell {label!r} has {len(shown)} element children"


# ── Clamped lists and inline styles ───────────────────────────────────

class _Clamps(HTMLParser):
    """Collects every .m-clamp (with its direct-child count), every
    .m-showall button (with whether a .m-clamp-wrap encloses it), every
    .m-unclamp element's classes, and counts .m-clamp-wraps, including any
    nested in another."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []        # [tag, classes, clamp_ref]
        self.clamps = []
        self.showall = []
        self.unclamped = []
        self.wraps = 0
        self.nested_wraps = 0
        self._btn = None

    def handle_starttag(self, tag, attrs):
        a = {k: (v if v is not None else "") for k, v in attrs}
        cls = a.get("class", "").split()
        if self.stack and self.stack[-1][2] is not None:
            self.stack[-1][2]["children"] += 1
        in_wrap = any("m-clamp-wrap" in e[1] for e in self.stack)
        if "m-clamp-wrap" in cls:
            self.wraps += 1
            self.nested_wraps += in_wrap
        ref = None
        if "m-clamp" in cls:
            ref = {"children": 0, "classes": cls}
            self.clamps.append(ref)
        if "m-unclamp" in cls:
            self.unclamped.append(cls)
        if "m-showall" in cls:
            self._btn = {"in_wrap": in_wrap, "attrs": a, "text": ""}
            self.showall.append(self._btn)
        if tag not in VOID:
            self.stack.append([tag, cls, ref])

    def handle_endtag(self, tag):
        if tag == "button":
            self._btn = None
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        if self._btn is not None:
            self._btn["text"] += data


def clamps(html):
    """The parsed _Clamps: .clamps, .showall (text normalised), .unclamped,
    .wraps, .nested_wraps."""
    p = _Clamps()
    p.feed(html)
    p.close()
    for b in p.showall:
        b["text"] = norm(b["text"])
    return p


class Styled(HTMLParser):
    """Every start tag's name, classes and inline style."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = []

    def handle_starttag(self, tag, attrs):
        a = {k: (v if v is not None else "") for k, v in attrs}
        self.tags.append((tag, a.get("class", "").split(), a.get("style", "")))


# ── site.css sections and rules ───────────────────────────────────────

def css_section(task, css=None):
    """The text of R2 task `task`'s section of site.css ("T1"…"T6", or "P"
    for the polish pass): what lies between `/* ── R2 <task> · … ── */` and
    `/* ── end R2 <task> ── */`, with comments stripped. Reads SITE_CSS
    unless `css` is given. Fails (AssertionError) unless the header and end
    marker each appear exactly once, header first."""
    if css is None:
        with open(SITE_CSS, encoding="utf-8") as fh:
            css = fh.read()
    heads = list(re.finditer(rf"/\* ── R2 {re.escape(task)} · [^*]*? ── \*/", css))
    end = f"/* ── end R2 {task} ── */"
    assert len(heads) == 1, f"expected one R2 {task} section header in site.css, found {len(heads)}"
    assert css.count(end) == 1, f"expected one {end!r} in site.css, found {css.count(end)}"
    start, stop = heads[0].end(), css.index(end)
    assert start <= stop, f"R2 {task}'s end marker comes before its header"
    return re.sub(r"/\*.*?\*/", "", css[start:stop], flags=re.S)


def phone_block(section):
    """Split an R2 section (css_section's text) into the body of its phone
    @media block, found by matching braces, and whatever follows that block
    up to the end marker: a desktop rule, if the task needed one. Fails
    (AssertionError) unless the section opens with
    `@media (max-width: 640px) {` and that block is closed."""
    m = re.match(r"\s*@media \(max-width: 640px\) \{", section)
    assert m, "the section must open with its phone @media block"
    depth, i = 1, m.end()
    while depth:
        assert i < len(section), "the phone @media block is never closed"
        depth += (section[i] == "{") - (section[i] == "}")
        i += 1
    return section[m.end():i - 1], section[i:]


def selectors(prelude):
    """Split a selector list on its top-level commas (not those in :not())."""
    out, depth, cur = [], 0, ""
    for ch in prelude:
        depth += (ch == "(") - (ch == ")")
        if ch == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    return out + [cur.strip()]


def rule_bodies(css, selector):
    """Joined bodies of every innermost rule in `css` whose selector list
    contains `selector` exactly (as split by selectors()). A css_section
    holds its phone block and any desktop rule after it, so for phone-only
    bodies, slice up to the @media block's closing brace (phone_block)."""
    out = []
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        if selector in selectors(m.group(1)):
            out.append(m.group(2))
    return "\n".join(out)
