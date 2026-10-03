"""Shared assertions for the mobile m-row contract (mobile design §4.3).

`assert_mrow(html)` parses rendered HTML and checks every element with class
`m-row`:
  * 1–2 direct children with data-m="key"
  * at most 1 direct child with data-m="lead"
  * every data-m-label is non-empty
  * a non-link row is toggled by toggleMRow or by its own toggleExpanded;
    a link row (m-row--link) is never toggled by toggleMRow
It returns the parsed rows so a test can make page-specific checks too."""
from html.parser import HTMLParser

_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
         "meta", "source", "track", "wbr"}


class _Collector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []   # (tag, row_index_or_None)
        self.rows = []    # {"tag", "attrs", "children": [attrs...]}

    def handle_starttag(self, tag, attrs):
        a = {k: (v if v is not None else "") for k, v in attrs}
        if self.stack and self.stack[-1][1] is not None:
            self.rows[self.stack[-1][1]]["children"].append(a)
        idx = None
        if "m-row" in a.get("class", "").split():
            self.rows.append({"tag": tag, "attrs": a, "children": []})
            idx = len(self.rows) - 1
        if tag not in _VOID:
            self.stack.append((tag, idx))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                return


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
