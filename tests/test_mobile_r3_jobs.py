"""Mobile R3 T3: Active Jobs (/industry/jobs) at phone width.

The eight-column jobs table becomes expand-on-tap rows (D7 A: key 2 is the
time left) and lists every job (D8 A: no clamp; the filters narrow it). The
desktop cells stay exactly as they were: every phone cell is an extra
`m-only` <td> after the eighth desktop cell, so the tablet rule that hides
columns 5 and 6 by nth-child still finds Installer and Location.

The client-side filters used to hide rows with an inline display:none,
which R1's `.m-row { display:grid !important }` beats on phones. They now
set the `hidden` attribute, and this task's site.css section hides an
m-row with it.

Contexts follow the shapes app/routes/industry_jobs.py builds; every name
and id is invented."""
import functools
import re
from html.parser import HTMLParser

from app.routes import industry_jobs as jobs_mod
from tests._mobile import (SITE_CSS, assert_mrow, assert_single_value_child,
                           cells_rows, clamps, css_section, phone_block, row_keys,
                           row_labelled, row_lead, rule_bodies, render_page, source)

_section = functools.partial(css_section, release="R3")

_LABELS = ["Name", "Activity", "Runs", "Source", "Installer", "Location", "Status"]


def _job(**kw):
    row = {
        "job_id": 1, "activity_id": 1, "activity_label": "Manufacturing", "activity_short": "Manuf.",
        "product_id": 990001, "product_name": "Sample Armor Plate",
        "blueprint_name": "Sample Armor Plate Blueprint", "runs": 10,
        "installer_id": 90000001, "installer_name": "Pilot Alpha",
        "location_id": 60000001, "location_name": "Sample Station",
        "source_kind": "character", "source_id": 90000001, "source_name": "Pilot Alpha",
        "time_remaining": "Ready", "urgency": "ready", "end_iso": "2026-10-01T10:00:00Z",
        "end_sort": 0.0, "status": "ready",
    }
    row.update(kw)
    return row


# Character and corp jobs, ready and active, four activities. Job 1's
# blueprint name differs from its product; job 4 has no product icon, no
# installer and no location.
_JOBS = [
    _job(),
    _job(job_id=2, activity_id=8, activity_label="Invention", activity_short="Invention",
         product_id=990002, product_name="Sample Drone Blueprint", blueprint_name="Sample Drone Blueprint",
         runs=3, installer_id=90000002, installer_name="Pilot Bravo",
         source_kind="corporation", source_id=98000001, source_name="Sample Holding",
         location_name="Sample Fortress", time_remaining="45m", urgency="soon",
         status="active"),
    _job(job_id=3, activity_id=5, activity_label="Copying", activity_short="Copy",
         product_id=990003, product_name="Sample Hull Blueprint", blueprint_name="Sample Hull Blueprint",
         runs=1, installer_id=90000001, installer_name="Pilot Alpha",
         source_kind="corporation", source_id=98000001, source_name="Sample Holding",
         time_remaining="2d 3h", urgency="normal", status="active"),
    _job(job_id=4, activity_id=9, activity_label="Reactions", activity_short="React.",
         product_id=None, product_name="—", blueprint_name=None, runs=20,
         installer_id=None, installer_name=None, location_id=None, location_name=None,
         time_remaining="5h 10m", urgency="normal", status="active"),
]


def _render(jobs=_JOBS):
    return render_page(
        jobs_mod, "industry_jobs.html", "/industry/jobs",
        rows=jobs,
        counts_by_activity=[("Manufacturing", 1), ("Invention", 1), ("Copying", 1), ("Reactions", 1)],
        counts_by_source={"character": 2, "corporation": 2},
        counts_by_status={"ready": 1, "active": 3},
        character_filters=[{"id": 90000001, "name": "Pilot Alpha", "count": 2},
                           {"id": 90000002, "name": "Pilot Bravo", "count": 1}],
        corp_filters=[{"id": 98000001, "name": "Sample Holding", "count": 2}],
        total=len(jobs), include_completed=False,
        char_count_with_scope=2, corp_count_with_scope=1, npc_corps_skipped=[],
        perm_chars=[], perm_corp_chars=[], corp_roles={})


class _Tags(HTMLParser):
    """Every start tag as (tag, attrs), in document order."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, {k: (v if v is not None else "") for k, v in attrs}))


def _tags(html):
    p = _Tags()
    p.feed(html)
    return p.tags


def _classes(attrs):
    return attrs.get("class", "").split()


def _rows(html=None):
    """The four job rows, failing (rather than passing vacuously) if any is
    missing."""
    rows = cells_rows(html if html is not None else _render())
    assert len(rows) == len(_JOBS), f"expected {len(_JOBS)} job rows, found {len(rows)}"
    return rows


def _script(html):
    m = re.search(r"<script nonce=\"test-nonce\">((?:(?!</script>).)*applyFilters(?:(?!</script>).)*)</script>",
                  html, flags=re.S)
    assert m, "the jobs filter script is missing"
    return m.group(1)


# ── Table and rows ────────────────────────────────────────────────────


def test_jobs_table_is_an_m_table_with_an_m_head():
    tags = _tags(_render())
    tables = [a for t, a in tags if t == "table"]
    assert len(tables) == 1
    assert _classes(tables[0]) == ["ij-table", "m-table"]   # the JS still finds .ij-table
    i = next(n for n, (t, _) in enumerate(tags) if t == "thead")
    tag, attrs = tags[i + 1]
    assert tag == "tr" and "m-head" in _classes(attrs)


def test_each_job_is_an_expand_on_tap_row():
    html = _render()
    rows = assert_mrow(html, min_rows=4)
    assert len(rows) == 4
    for r in rows:
        assert r["tag"] == "tr"
        assert r["attrs"]["class"] == "m-row"
        assert r["attrs"]["data-click"] == "toggleMRow"


def test_row_data_attributes_are_unchanged():
    """The filters read these four attributes from each row."""
    rows = cells_rows(_render())
    got = [{k: r["attrs"][k] for k in ("data-activity", "data-source", "data-installer-id", "data-corp-id")}
           for r in rows]
    assert got == [
        {"data-activity": "Manufacturing", "data-source": "character",
         "data-installer-id": "90000001", "data-corp-id": ""},
        {"data-activity": "Invention", "data-source": "corporation",
         "data-installer-id": "90000002", "data-corp-id": "98000001"},
        {"data-activity": "Copying", "data-source": "corporation",
         "data-installer-id": "90000001", "data-corp-id": "98000001"},
        {"data-activity": "Reactions", "data-source": "character",
         "data-installer-id": "", "data-corp-id": ""},
    ]


def test_desktop_cells_come_first_and_stay_untagged():
    """Desktop renders identically: the eight desktop cells keep their
    order and content and carry no phone tag, and every phone cell is an
    m-only <td> after them. A <td>, because a browser moves any other
    element out of a <tr>; after them, so the tablet rule's nth-child(5)
    and nth-child(6) are still Installer and Location."""
    for job, row in zip(_JOBS, _rows()):
        cells = row["cells"]
        assert all(c["tag"] == "td" for c in cells)
        desktop, phone = cells[:8], cells[8:]
        for c in desktop:
            assert "m-only" not in _classes(c["attrs"])
            assert "data-m" not in c["attrs"] and "data-m-label" not in c["attrs"]
        assert desktop[0]["text"].startswith(job["product_name"])
        assert desktop[1]["text"] == job["activity_short"]
        assert desktop[2]["text"] == str(job["runs"])
        assert desktop[3]["text"] == f"{job['source_kind'].capitalize()} {job['source_name']}"
        assert desktop[4]["text"] == (job["installer_name"] or "—")
        assert desktop[5]["text"] == (job["location_name"] or "—")
        assert desktop[6]["text"] == job["time_remaining"]
        assert desktop[6]["attrs"]["class"] == f"ij-time-{job['urgency']}"
        assert desktop[7]["text"] == job["status"]
        assert phone, "no phone cells"
        for c in phone:
            assert _classes(c["attrs"]) == ["m-only"]
            assert "data-m" in c["attrs"] or "data-m-label" in c["attrs"]


def test_lead_is_the_product_icon():
    for job, row in zip(_JOBS, _rows()):
        lead = row_lead(row)
        assert len(lead) == 1
        if job["product_id"] is None:
            assert lead[0]["kids"] == []      # the cell stays, so key 1 keeps its place
            continue
        (img,) = lead[0]["kids"]
        assert img["src"] == f"https://images.evetech.net/types/{job['product_id']}/icon?size=32"
        assert (img["width"], img["height"]) == ("24", "24")   # holds its size before it loads
        assert img["loading"] == "lazy"                         # hidden on desktop: never fetched there
        assert img["data-on-error"] == "hide"                   # hides the img, not the tagged cell
        assert img["alt"] == ""


def test_key_1_is_the_product_and_key_2_the_time_left():
    """D7 A. Key 2 keeps the desktop colour coding on a child span: the
    page's `.ij-table tbody td { color }` outranks `.ij-time-ready` on the
    cell itself."""
    for job, row in zip(_JOBS, _rows()):
        k1, k2 = row_keys(row)
        assert k1["text"] == job["product_name"]
        assert k2["text"] == job["time_remaining"]
        assert "class" not in k2["attrs"] or not any(c.startswith("ij-time-") for c in _classes(k2["attrs"]))
        (span,) = k2["kids"]
        assert _classes(span) == [f"ij-time-{job['urgency']}"]


def test_labelled_cells_start_with_name_in_brief_order():
    for row in _rows():
        assert list(row_labelled(row)) == _LABELS
        assert_single_value_child(row)


def test_labelled_values_match_the_desktop_columns():
    for job, row in zip(_JOBS, _rows()):
        cells = row_labelled(row)
        bp_line = job["blueprint_name"] and job["blueprint_name"] != job["product_name"]
        name = job["product_name"] + (f" from {job['blueprint_name']}" if bp_line else "")
        assert cells["Name"]["text"] == name
        # The full name, as the filter chips show it; the desktop column
        # keeps the short form ("Manuf.").
        assert cells["Activity"]["text"] == job["activity_label"]
        assert cells["Runs"]["text"] == str(job["runs"])
        assert cells["Source"]["text"] == f"{job['source_kind'].capitalize()} {job['source_name']}"
        assert cells["Installer"]["text"] == (job["installer_name"] or "—")
        assert cells["Location"]["text"] == (job["location_name"] or "—")
        assert cells["Status"]["text"] == job["status"]
        (badge,) = cells["Status"]["kids"]
        assert _classes(badge) == ["ij-status", f"is-{job['status']}"]


def test_name_line_carries_the_blueprint_sub_line():
    """The desktop product cell shows "from <blueprint>" when it differs;
    the open row's Name line keeps it, inside the one value wrapper."""
    html = _render()
    first = cells_rows(html)[0]
    (wrapper,) = row_labelled(first)["Name"]["kids"]
    assert wrapper == {}
    assert re.search(r'data-m-label="Name"><span>Sample Armor Plate <span class="ij-sub">'
                     r'from Sample Armor Plate Blueprint</span></span></td>', html)
    for row in cells_rows(html)[1:]:
        assert "from" not in row_labelled(row)["Name"]["text"]


def test_every_job_is_listed_without_a_clamp():
    """D8 A: list everything; the filters do the narrowing."""
    many = [_job(job_id=n) for n in range(1, 25)]
    html = _render(many)
    assert len(assert_mrow(html, min_rows=24)) == 24
    c = clamps(html)
    assert c.clamps == [] and c.showall == [] and c.wraps == 0


def test_filter_hooks_are_unchanged():
    tags = _tags(_render())
    ids = {a.get("id") for _, a in tags}
    assert {"picker-installer", "picker-corp", "ij-clear-filters"} <= ids
    attrs = [a for _, a in tags]
    assert [a["data-filter-activity"] for a in attrs if "data-filter-activity" in a] == [
        "all", "Manufacturing", "Invention", "Copying", "Reactions"]
    assert [a["data-filter-source"] for a in attrs if "data-filter-source" in a] == [
        "all", "character", "corporation"]
    assert [a["data-filter-installer-id"] for a in attrs if "data-filter-installer-id" in a] == [
        "90000001", "90000002"]
    assert [a["data-filter-corp-id"] for a in attrs if "data-filter-corp-id" in a] == ["98000001"]
    for kind in ("installer", "corp"):
        for hook in ("data-picker-toggle", "data-picker-all", "data-picker-none"):
            assert any(a.get(hook) == kind for a in attrs), (hook, kind)


# ── Filters: the hidden attribute, not an inline display ──────────────


def test_filters_hide_rows_with_the_hidden_attribute():
    """`.m-row { display:grid !important }` beats an inline display:none, so
    the filters set `hidden` instead (the phone rule below honours it). No
    other code read the old inline style: nothing counts visible rows."""
    script = _script(_render())
    assert "style.display" not in script
    assert ".ij-table tbody tr" in script          # the rows the filters walk
    assert re.search(r"\btr\.hidden\s*=", script)


# ── Tablet rule (template <style>) ────────────────────────────────────


def test_tablet_column_rule_no_longer_reaches_phones():
    """Columns 5 and 6 (Installer, Location) still hide between 641 and
    900px. Below that the jobs are m-rows, so the rule stops at 641px and
    can never hide a phone cell."""
    src = source("industry_jobs.html")
    style = src[src.index("<style"):src.index("</style>")]
    queries = re.findall(r"@media\s*([^{]*)\{", style)
    assert [q.strip() for q in queries] == ["(min-width:641px) and (max-width:900px)"]
    body = style[style.index("@media"):]
    for n in (5, 6):
        assert f".ij-table thead th:nth-child({n})" in body
        assert f".ij-table tbody td:nth-child({n})" in body
    assert "display:none;" in body


# ── site.css: this task's section ─────────────────────────────────────


def _phone():
    phone, after = phone_block(_section("T3"))
    assert after.strip() == "", "no desktop rule: desktop renders as before"
    return phone


def _decls(body):
    return {k.strip(): v.strip() for k, v in
            (d.split(":", 1) for d in body.split(";") if ":" in d)}


def test_hidden_rows_stay_hidden_on_phones():
    """Both rules are !important, so specificity decides: (0,3,1) here beats
    R1's `.m-row` (0,1,0). The section also comes after that rule."""
    assert _decls(rule_bodies(_phone(), ".ij-table tr.m-row[hidden]")) == {"display": "none !important"}
    with open(SITE_CSS, encoding="utf-8") as fh:
        raw = fh.read()
    grid = raw.index(".m-row {\n        display: grid !important;")
    assert grid < raw.index("/* ── R3 T3 · active jobs ── */")


def test_row_draws_one_divider_instead_of_each_cell():
    """The page's `.ij-table tbody td` (0,1,2) pads and underlines every
    cell, and its <style> loads after site.css, so the reset needs two
    classes."""
    phone = _phone()
    assert _decls(rule_bodies(phone, ".ij-table tr.m-row")) == {"border-bottom": "1px solid var(--border)"}
    assert _decls(rule_bodies(phone, ".ij-table tr.m-row > td")) == {"padding": "0", "border-bottom": "none"}


def test_keys_and_lead_on_phones():
    phone = _phone()
    assert _decls(rule_bodies(phone, '.ij-table tr.m-row > [data-m="key"]')) == {"font-size": "12px"}
    lead = _decls(rule_bodies(phone, '.ij-table tr.m-row > [data-m="lead"]'))
    # Beats R1's `.m-row > * { min-width: 0 !important }`: a job with no
    # icon, or a broken one, keeps the 24px slot.
    assert lead["min-width"] == "24px !important"
    assert _decls(rule_bodies(phone, ".ij-table tr.m-row > [data-m-label] .ij-sub")) == {"display": "block"}


def test_picker_panel_fits_a_phone_with_40px_rows():
    """The panel spans the filter row (not 220px from wherever its button
    wrapped to), and each option row is a 40px target. The global
    `input { min-height: 44px }` would otherwise size each row by its
    checkbox."""
    phone = _phone()
    assert _decls(rule_bodies(phone, ".ij-filters")) == {"position": "relative"}
    assert _decls(rule_bodies(phone, ".ij-filters .ij-picker")) == {"position": "static"}
    assert _decls(rule_bodies(phone, ".ij-filters .ij-picker-panel")) == {
        "left": "0", "right": "0", "min-width": "0"}
    row = _decls(rule_bodies(phone, ".ij-picker-panel .ij-picker-row"))
    assert row["min-height"] == "40px"
    assert _decls(rule_bodies(phone, ".ij-picker-panel .ij-picker-row input")) == {"min-height": "0"}


def test_small_text_is_11px_over_the_page_style():
    """The stat labels, the pickers' All / None buttons and each option's
    count are 9px in the page's <style>, which loads after site.css; each
    phone rule carries one more class than the page's, so it wins."""
    phone = _phone()
    page = source("industry_jobs.html")
    for sel, page_sel in ((".ij-summary .ij-stat-label", ".ij-stat-label"),
                          (".ij-picker-panel > .ij-picker-head > button", ".ij-picker-head button"),
                          (".ij-picker-panel .ij-picker-row .pcount", ".ij-picker-row .pcount")):
        assert _decls(rule_bodies(phone, sel)) == {"font-size": "11px"}, sel
        assert re.search(r"(?m)^" + re.escape(page_sel) + r" \{[^}]*font-size:9px", page), page_sel
    # The markup the rules name: stats inside the summary, both pickers'
    # heads directly inside their panels, buttons directly inside the heads.
    html = _render()
    assert re.search(r'<div class="ij-summary">\s*<div class="ij-stat"><span class="ij-stat-label">', html)
    heads = re.findall(r'<div class="ij-picker-panel">\s*<div class="ij-picker-head">\s*'
                       r'<button type="button" data-picker-all="\w+">All</button>\s*'
                       r'<button type="button" data-picker-none="\w+">None</button>', html)
    assert len(heads) == 2
    assert html.count('<span class="pcount">') == 3


def test_include_completed_link_is_a_40px_target():
    """"+ Include completed" and "Including completed ×" are links styled as
    chips, 24.5px tall at 360px. As inline-flex boxes they take a 40px
    height with the label centred. The filter chips are buttons, which the
    global phone rule already makes 44px tall."""
    assert _decls(rule_bodies(_phone(), "a.ij-chip")) == {
        "display": "inline-flex", "align-items": "center", "min-height": "40px"}
    links = re.findall(r'<a href="([^"]*)" class="ij-chip[^"]*"', source("industry_jobs.html"))
    assert links == ["?", "?include_completed=1"]
