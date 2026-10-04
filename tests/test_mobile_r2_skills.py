"""Mobile R2 T2: the Skills planner page at phone width.

User decisions:
  D4 B  Skill Queue rows are m-rows (key 1 skill + level, key 2 time,
        labelled Attributes / SP), the header is m-head, and the list shows
        its first 10 rows plus "Show all N" on phones.
  D5 A  Remap per-skill comparison rows are m-rows: key 2 is the Diff, in
        its own colour; labelled Attributes / Current / Remapped. They arrive
        by htmx swap, where R1's mRowInit already runs.
  D6 A  The Queue Summary and Remap Summary 3-tile rows become label/value
        lines on phones.
The What-If sliders stack one per line on phones (m-stack).

skills.html and partials/remap_results.html are rendered through the route
module's own templates.env with contexts shaped like skills.py builds them
(skill_planner() and remap_calculate()). Names and ids are invented."""
import re
from html.parser import HTMLParser

import pytest

from app.routes import skills as skills_mod
from tests._mobile import (VOID, assert_mrow, assert_single_value_child, cells_rows,
                           clamps, css_section, mrows, norm, render_page, row_keys,
                           row_labelled, rule_bodies)

_ROMAN = ["", "I", "II", "III", "IV", "V"]
_NAMES = skills_mod.ATTR_NAMES
# Longer than key 1 is wide at 360px (~28 mono characters), so a phone
# check of this fixture shows whether the level survives truncation.
_LONG = "Sample Heavy Assault Missile Specialization"


# ── fixtures ──────────────────────────────────────────────────────────

def _queue_item(i, name, level, primary=1, secondary=2, rank=3, sp=12345, time_str="1d 2h 3m"):
    """One entry as skill_planner() builds it."""
    return {"skill_id": 3300 + i, "name": name, "level": level, "rank": rank,
            "primary": primary, "secondary": secondary,
            "primary_name": _NAMES[primary], "secondary_name": _NAMES[secondary],
            "sp_needed": sp, "time_str": time_str, "time_minutes": 1563.0}


def _queue(n):
    items = [_queue_item(0, _LONG, 5, primary=3, secondary=4, rank=8, sp=1234567,
                         time_str="12d 3h 4m")]
    for i in range(1, n):
        items.append(_queue_item(i, f"Sample Skill {i}", (i % 5) + 1))
    return items[:n]


def _render_skills(n):
    queue = _queue(n)
    return render_page(
        skills_mod, "skills.html", "/character/90000001/skills",
        char={"character_id": 90000001, "character_name": "Sample Pilot",
              "corporation_name": "Sample Corp", "scopes": "esi-skills.read_skills.v1"},
        error=None, attributes=[17, 27, 21, 17, 17], attr_names=_NAMES,
        attr_keys=skills_mod.ATTR_KEYS, implants=[0, 0, 0, 0, 0], total_sp=12345678,
        queue_items=queue, total_current_minutes=57720.0, current_time_str="40d 2h 0m",
        optimal_attrs=[17, 27, 17, 21, 17], optimal_time=54840.0,
        optimal_time_str="38d 2h 0m", time_saved=2880.0, time_saved_str="2d 0h 0m",
        bonus_remaps=1, last_remap="", next_remap="2026-11-01"), queue


def _remap_row(name, level, current, proposed, diff_minutes, diff_str, primary=1, secondary=2):
    """One row as remap_calculate() builds it."""
    return {"name": name, "level": level,
            "primary_name": _NAMES[primary], "secondary_name": _NAMES[secondary],
            "current_time": current, "proposed_time": proposed,
            "diff_minutes": diff_minutes, "diff_str": diff_str, "faster": diff_minutes > 0}


# (row, expected key 2 text, expected key 2 colour)
_REMAP = [
    (_remap_row(_LONG, 5, "3d 4h 0m", "2d 1h 0m", 1620.0, "1d 3h 0m", 3, 4),
     "-1d 3h 0m", "var(--success)"),
    (_remap_row("Sample Skill Slower", 3, "1h 0m", "4h 0m", -180.0, "3h 0m"),
     "+3h 0m", "var(--danger)"),
    (_remap_row("Sample Skill Same", 1, "20m", "20m", 0.0, "done"),
     "—", "var(--muted)"),
]


def _render_remap(time_diff=17460.0, time_diff_str="12d 3h 4m"):
    return render_page(
        skills_mod, "partials/remap_results.html", "/character/90000001/skills/remap-calc",
        rows=[r for r, _, _ in _REMAP], proposed=[17, 27, 17, 21, 17], total_points=99,
        valid=True, current_total_str="40d 2h 0m", proposed_total_str="27d 22h 56m",
        time_diff=time_diff, time_diff_str=time_diff_str, is_faster=time_diff > 0,
        attr_names=_NAMES)


# ── a minimal element tree, for structure checks ──────────────────────

class _Node:
    def __init__(self, tag, attrs, parent):
        self.tag, self.attrs, self.parent = tag, attrs, parent
        self.children, self.text = [], ""

    @property
    def classes(self):
        return self.attrs.get("class", "").split()

    def all(self):
        for c in self.children:
            yield c
            yield from c.all()

    def find(self, cls):
        return [n for n in self.all() if cls in n.classes]

    def full_text(self):
        return norm(self.text + " ".join(c.full_text() for c in self.children))


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
        while n is not self.root and n.tag != tag:
            n = n.parent
        if n is not self.root:
            self.cur = n.parent

    def handle_data(self, data):
        self.cur.text += data


def _tree(html):
    t = _Tree()
    t.feed(html)
    t.close()
    return t.root


# ── D4 B: Skill Queue rows ────────────────────────────────────────────

@pytest.mark.parametrize("n", [3, 12])
def test_queue_rows_follow_the_contract(n):
    html, queue = _render_skills(n)
    assert len(mrows(html)) == n, "every m-row on the page is a queue row"
    assert_mrow(html, min_rows=n)
    rows = cells_rows(html)
    for q, row in zip(queue, rows):
        assert row["attrs"].get("data-click") == "toggleMRow"
        k1, k2 = row_keys(row)
        assert k1["text"] == f"{q['name']} {_ROMAN[q['level']]}"
        assert k2["text"] == q["time_str"]
        labelled = row_labelled(row)
        assert set(labelled) == {"Attributes", "SP"}
        assert labelled["Attributes"]["text"] == (
            f"{q['primary_name'][:3]}/{q['secondary_name'][:3]} ×{q['rank']}")
        assert labelled["SP"]["text"] == "{:,}".format(q["sp_needed"])
        assert_single_value_child(row)


def test_queue_key_one_keeps_name_and_level_as_separate_children():
    """Key 1's phone rule ellipsises the name and keeps the level whole, so
    the level of a long skill name stays visible. That needs the name and
    the level as the key cell's two element children, and the hook class."""
    html, queue = _render_skills(3)
    rows = cells_rows(html)
    assert len(rows) == 3
    for row in rows:
        k1 = row_keys(row)[0]
        assert "skills-key" in k1["attrs"].get("class", "").split()
        assert len(k1["kids"]) == 2


@pytest.mark.parametrize("n, button", [(3, False), (10, False), (11, True), (12, True)])
def test_queue_shows_all_past_ten(n, button):
    html, _ = _render_skills(n)
    c = clamps(html)
    assert c.wraps == 1 and c.nested_wraps == 0
    assert len(c.clamps) == 1
    assert c.clamps[0]["children"] == n, "the clamp holds the queue rows and nothing else"
    if button:
        assert len(c.showall) == 1
        b = c.showall[0]
        assert b["in_wrap"]
        assert b["text"] == f"Show all {n}"
        assert {"m-only", "m-showall"} <= set(b["attrs"]["class"].split())
        assert b["attrs"].get("type") == "button"
        assert b["attrs"].get("data-click") == "toggleExpanded"
        assert b["attrs"].get("data-toggle-target") == ".m-clamp-wrap"
    else:
        assert c.showall == []


def test_queue_rows_sit_directly_in_the_clamp_not_the_wrap():
    """Show all expands the wrap. R1 opens a row whose direct parent is
    is-expanded, so the rows' parent must be the clamp list, not the wrap.
    The button follows the list inside the wrap, so the last row stays its
    list's last child (desktop drops that row's bottom border)."""
    root = _tree(_render_skills(12)[0])
    wrap, = root.find("m-clamp-wrap")
    assert "skills-queue" in wrap.classes
    clamp, button = wrap.children
    assert "m-clamp" in clamp.classes and "m-showall" in button.classes
    assert all("m-row" in r.classes for r in clamp.children)


def test_empty_queue_renders_no_clamp():
    html, _ = _render_skills(0)
    assert "No active skills in queue" in html
    assert clamps(html).wraps == 0
    assert mrows(html) == []


def test_queue_header_is_m_head():
    root = _tree(_render_skills(3)[0])
    heads = root.find("m-head")
    assert len(heads) == 1
    head = heads[0]
    assert "b-table-row" in head.classes
    assert [c.full_text() for c in head.children] == ["Skill", "Attributes", "SP", "Time"]


# ── D5 A: Remap per-skill comparison ──────────────────────────────────

def test_remap_rows_follow_the_contract():
    html = _render_remap()
    assert len(mrows(html)) == len(_REMAP)
    assert_mrow(html, min_rows=len(_REMAP))
    for (r, diff_text, diff_colour), row in zip(_REMAP, cells_rows(html)):
        assert row["attrs"].get("data-click") == "toggleMRow"
        k1, k2 = row_keys(row)
        assert k1["text"] == f"{r['name']} {_ROMAN[r['level']]}"
        assert "skills-key" in k1["attrs"].get("class", "").split()
        assert len(k1["kids"]) == 2, "name and level are the key cell's two children"
        assert k2["text"] == diff_text
        assert f"color:{diff_colour}" in k2["attrs"]["style"].replace(" ", "")
        labelled = row_labelled(row)
        assert set(labelled) == {"Attributes", "Current", "Remapped"}
        assert labelled["Attributes"]["text"] == (
            f"{r['primary_name'][:3]}/{r['secondary_name'][:3]}")
        assert labelled["Current"]["text"] == r["current_time"]
        assert labelled["Remapped"]["text"] == r["proposed_time"]
        remapped = "var(--success)" if r["faster"] else "var(--text)"
        assert f"color:{remapped}" in labelled["Remapped"]["attrs"]["style"].replace(" ", "")
        assert_single_value_child(row)


def test_remap_header_is_m_head():
    root = _tree(_render_remap())
    heads = root.find("m-head")
    assert len(heads) == 1
    assert "b-table-row" in heads[0].classes
    assert [c.full_text() for c in heads[0].children] == [
        "Skill", "Attrs", "Current", "Remapped", "Diff"]


def test_invalid_remap_renders_no_rows():
    html = render_page(skills_mod, "partials/remap_results.html", "/", rows=[],
                       valid=False, total_points=101)
    assert "Invalid remap" in html
    assert mrows(html) == []


# ── D6 A: summary tiles ───────────────────────────────────────────────

def _tiles(root):
    """Each .skills-tiles row as [(label text, value text, value style)]."""
    out = []
    for row in root.find("skills-tiles"):
        assert all("skills-tile" in t.classes for t in row.children)
        lines = []
        for tile in row.children:
            labels = [c for c in tile.children if "skills-tile-label" in c.classes]
            values = [c for c in tile.children if "skills-tile-value" in c.classes]
            assert len(labels) == 1 and len(values) == 1 and len(tile.children) == 2
            lines.append((labels[0].full_text(), values[0].full_text(),
                          values[0].attrs.get("style", "").replace(" ", "")))
        out.append(lines)
    return out


def test_queue_summary_tiles_have_hooks():
    root = _tree(_render_skills(3)[0])
    rows = _tiles(root)
    assert len(rows) == 1
    (skills, current, optimal), = rows
    assert skills[:2] == ("Skills", "3")
    assert current[:2] == ("Current", "40d 2h 0m")
    assert optimal[:2] == ("Optimal", "38d 2h 0m")
    assert "color:var(--success)" in optimal[2]
    assert "font-weight:300" in optimal[2]


@pytest.mark.parametrize("time_diff, text, colour", [
    (17460.0, "-12d 3h 4m faster", "var(--success)"),
    (-17460.0, "+12d 3h 4m slower", "var(--danger)"),
    (0.0, "no change", "var(--muted)"),
])
def test_remap_summary_tiles_have_hooks(time_diff, text, colour):
    root = _tree(_render_remap(time_diff=time_diff))
    rows = _tiles(root)
    assert len(rows) == 1
    (current, after, diff), = rows
    assert current[:2] == ("Current", "40d 2h 0m")
    assert after[:2] == ("After Remap", "27d 22h 56m")
    assert diff[:2] == ("Difference", text)
    assert f"color:{colour}" in diff[2] and "font-weight:600" in diff[2]


# ── What-If sliders ───────────────────────────────────────────────────

def test_what_if_sliders_stack_one_per_line():
    root = _tree(_render_skills(3)[0])
    stacks = [n for n in root.find("m-stack")
              if any(d.attrs.get("type") == "range" for d in n.all())]
    assert len(stacks) == 1
    stack = stacks[0]
    assert len(stack.children) == 5
    for i, slider in enumerate(stack.children):
        assert [d.attrs.get("id") for d in slider.all() if d.tag == "input"] == [f"remap-{i}"]
    # The Apply Optimal / Reset row stays as it is.
    buttons = [n for n in root.all() if n.attrs.get("data-click") == "applyOptimal"]
    assert len(buttons) == 1
    assert "m-stack" not in buttons[0].parent.classes


# ── CSS: R2 T2 section ────────────────────────────────────────────────

def _decl(body, prop, value):
    return re.search(rf"(?:^|;|\s){re.escape(prop)}\s*:\s*{re.escape(value)}\s*(?:;|$)", body)


def test_t2_css_turns_summary_tiles_into_lines():
    css = css_section("T2")
    assert css.count("@media (max-width: 640px)") == 1
    row = rule_bodies(css, ".skills-tiles")
    assert _decl(row, "flex-direction", "column")
    tile = rule_bodies(css, ".skills-tiles > .skills-tile")
    for prop, value in (("display", "flex"), ("justify-content", "space-between"),
                        ("flex", "none !important"), ("border-right", "none !important"),
                        ("border-bottom", "1px solid var(--border)")):
        assert _decl(tile, prop, value), (prop, value)
    assert _decl(rule_bodies(css, ".skills-tiles > .skills-tile:last-child"),
                 "border-bottom", "none")
    label = rule_bodies(css, ".skills-tile > .skills-tile-label")
    # The label leads whichever DOM order a tile uses (the queue's tiles
    # put the value first).
    for prop, value in (("order", "-1"), ("flex", "none"), ("margin", "0 !important"),
                        ("text-align", "left")):
        assert _decl(label, prop, value), (prop, value)
    value = rule_bodies(css, ".skills-tile > .skills-tile-value")
    for prop, val in (("min-width", "0"), ("overflow-wrap", "anywhere"),
                      ("text-align", "right")):
        assert _decl(value, prop, val), (prop, val)


def test_t2_css_keeps_the_level_visible_in_key_one():
    css = css_section("T2")
    key = rule_bodies(css, '.m-row > [data-m="key"].skills-key')
    assert _decl(key, "display", "flex !important")
    name = rule_bodies(css, '.m-row > [data-m="key"].skills-key > :first-child')
    for prop, value in (("min-width", "0"), ("overflow", "hidden"),
                        ("text-overflow", "ellipsis"), ("white-space", "nowrap")):
        assert _decl(name, prop, value), (prop, value)
    assert _decl(rule_bodies(css, '.m-row > [data-m="key"].skills-key > :last-child'),
                 "flex", "none")


def test_t2_css_insets_show_all_inside_the_queue_panel():
    css = css_section("T2")
    body = rule_bodies(css, ".skills-queue .m-showall")
    assert _decl(body, "width", "calc(100% - 1.5rem)")
