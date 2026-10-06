"""Mobile R2 T6: the Skill Plan detail page, its gap check and the shared view
on phones (mobile design §4.3, §7; user decisions D13 A, D14 A, D15 A).

- Plan skill rows are m-rows: key 1 the skill name, key 2 the Roman target
  level. Attributes and (editable plans only) Remove open below the row; the
  drag handle stays untagged, so phones hide it.
- Custom-scope ACL rows: key 1 the name, key 2 a short type; Permission and
  Remove open below. The header is m-head and the add form stacks.
- Gap check rows: key 1 the status icon + name + level, key 2 the time (or
  "done"); Level ("cur → target") opens below.
- The scope bar's promote forms stack; the typeahead rows the route builds
  carry a class the phone CSS sizes; the shared view's "Check your
  character" row wraps. The shared view itself stays free of m-rows (it is
  public: T0's tests/test_mobile_r2_shared_plan.py guards its bindings).

Desktop must render exactly as before (D21): every phone-only cell is m-only
and every hook class has rules only inside the phone media block.

Contexts follow the shapes app/routes/skill_plans.py builds (plan_detail,
gap_analysis, shared_plan). Names and ids are invented."""
import asyncio
import re
import types
from html.parser import HTMLParser

from app.routes import skill_plans as sp_mod
from tests._mobile import (assert_mrow, assert_single_value_child, cells_rows, css_section,
                           mrows, render_page, row_keys, row_labelled, row_lead, rule_bodies,
                           selectors)

_NS = types.SimpleNamespace
_ROMAN = ["", "I", "II", "III", "IV", "V"]

_SKILLS = [
    ("Sample Gunnery Basics", 5, 1.0, "Perception", "Willpower"),
    ("Sample Hull Upgrades", 4, 2.0, "Intelligence", "Memory"),
    ("Sample Drone Operation", 3, 1.5, "Memory", "Perception"),
    ("Sample Shield Handling", 2, 3.0, "Intelligence", "Memory"),
    ("Sample Navigation Theory", 1, 1.0, "Intelligence", "Perception"),
    ("Sample Very Long Capital Ship Construction Skill Name", 5, 14.0, "Memory", "Intelligence"),
    ("Sample Target Painting", 4, 1.0, "Perception", "Willpower"),
    ("Sample Energy Grid", 3, 1.0, "Intelligence", "Memory"),
    ("Sample Warp Drive Tuning", 2, 2.5, "Intelligence", "Perception"),
    ("Sample Armor Layering", 1, 2.0, "Intelligence", "Memory"),
    ("Sample Signal Analysis", 5, 1.0, "Intelligence", "Memory"),
    ("Sample Afterburner Drills", 4, 1.0, "Intelligence", "Perception"),
]


def _entries(n=12):
    """Entries as plan_detail() / shared_plan() build them."""
    return [{"id": 500 + i, "skill_type_id": 3300 + i, "skill_name": name, "target_level": lvl,
             "rank": rank, "primary_attr_name": prim, "secondary_attr_name": sec}
            for i, (name, lvl, rank, prim, sec) in enumerate(_SKILLS[:n])]


def _plan(visibility="personal", share_token=None):
    return _NS(id=42, name="Sample Fleet Plan", visibility=visibility, owner_corp_id=None,
               owner_alliance_id=None, share_token=share_token, description="")


_CHARS = [_NS(character_id=90000101, character_name="Sample Pilot One"),
          _NS(character_id=90000102, character_name="Sample Pilot Two")]

_ACL = [
    _NS(id=1, subject_type="alliance", subject_id=99000001, subject_name="Sample Alliance", permission="view"),
    _NS(id=2, subject_type="character", subject_id=90000201, subject_name="Sample Friend", permission="edit"),
    _NS(id=3, subject_type="corporation", subject_id=98000001, subject_name="Sample Corp", permission="admin"),
]


def _render_detail(*, can_edit=True, can_admin=False, visibility="personal", entries=None,
                   acl_entries=None, eligible_corps=(), eligible_alliances=()):
    return render_page(
        sp_mod, "skill_plan_detail.html", "/skill-plans/42",
        plan=_plan(visibility), entries=_entries() if entries is None else entries,
        characters=_CHARS, corp_names={}, alliance_names={},
        can_edit=can_edit, can_admin=can_admin, is_owner=can_admin,
        eligible_corps=list(eligible_corps), eligible_alliances=list(eligible_alliances),
        acl_entries=list(acl_entries or []), acl_err=None)


def _skill_rows(html):
    return [r for r in cells_rows(html) if "skill-row" in r["attrs"].get("class", "").split()]


def _acl_rows(html):
    return [r for r in cells_rows(html) if "skp-acl-row" in r["attrs"].get("class", "").split()]


def _classes(attrs):
    return attrs.get("class", "").split()


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
    p.close()
    return p.tags


# ── 1. Plan skill rows (D13 A) ────────────────────────────────────────

def test_editable_plan_rows_key_on_name_and_target_level():
    html = _render_detail(can_edit=True)
    assert_mrow(html, min_rows=12)
    rows = _skill_rows(html)
    assert len(rows) == 12
    for row, (name, lvl, *_rest) in zip(rows, _SKILLS):
        assert row["attrs"]["data-click"] == "toggleMRow"
        # The drag IIFE reads these: keep them on the row itself.
        assert row["attrs"].get("draggable") == "true"
        assert row["attrs"]["data-id"].isdigit()
        k1, k2 = row_keys(row)
        assert k1["text"] == name, "key 1 is the name only, without the level"
        assert k2["text"] == _ROMAN[lvl]
        assert "var(--accent)" in k2["attrs"].get("style", "")
        # Both keys are phone-only copies: the desktop name + level cell is
        # untouched (so desktop renders as before).
        assert "m-only" in _classes(k1["attrs"]) and "m-only" in _classes(k2["attrs"])
        assert not row_lead(row)
        labelled = row_labelled(row)
        assert list(labelled) == ["Name", "Attributes", "Remove"]
        assert labelled["Name"]["text"] == name
        assert_single_value_child(row)
        assert labelled["Remove"]["tag"] == "form"
        assert "/remove-skill/" in labelled["Remove"]["attrs"]["action"]


def test_editable_plan_drag_handle_and_desktop_cell_stay_untagged():
    rows = _skill_rows(_render_detail(can_edit=True))
    assert len(rows) == 12
    for row, (name, lvl, *_rest) in zip(rows, _SKILLS):
        untagged = [c for c in row["cells"]
                    if "data-m" not in c["attrs"] and "data-m-label" not in c["attrs"]]
        handle, desktop = untagged
        assert handle["attrs"].get("title") == "Drag to reorder"
        assert desktop["text"] == f"{name} {_ROMAN[lvl]}"
        assert "m-only" not in _classes(handle["attrs"]) + _classes(desktop["attrs"])


def test_editable_plan_remove_button_is_a_tap_target_keeping_its_x():
    rows = _skill_rows(_render_detail(can_edit=True))
    assert len(rows) == 12
    for row in rows:
        cell = row_labelled(row)["Remove"]
        (button,) = cell["kids"]
        assert "b-btn" in _classes(button)
        assert "m-tap" not in _classes(button), "m-tap's 40px would shrink the 44px button"
        assert cell["text"] == "×"


def test_attributes_value_is_one_wrapper_with_the_rank():
    row = _skill_rows(_render_detail(can_edit=True))[1]
    cell = row_labelled(row)["Attributes"]
    assert len(cell["kids"]) == 1
    assert cell["text"] == "Int/Mem ×2"


def test_read_only_plan_rows_have_only_the_attributes_cell():
    html = _render_detail(can_edit=False)
    assert_mrow(html, min_rows=12)
    rows = _skill_rows(html)
    assert len(rows) == 12
    for row, (name, lvl, *_rest) in zip(rows, _SKILLS):
        assert "draggable" not in row["attrs"]
        assert [k["text"] for k in row_keys(row)] == [name, _ROMAN[lvl]]
        assert list(row_labelled(row)) == ["Name", "Attributes"]
        assert_single_value_child(row)
    assert "/remove-skill/" not in html


def test_skill_list_is_not_clamped():
    """The plan's own list is the page's content, not a nested scroll box,
    so it isn't cut to 10 on phones."""
    html = _render_detail(can_edit=True)
    assert "m-clamp" not in html and "m-showall" not in html


# ── 2. Access list rows (D14 A) ───────────────────────────────────────

def test_acl_rows_key_on_name_and_short_type():
    html = _render_detail(can_admin=True, visibility="custom", acl_entries=_ACL)
    assert_mrow(html, min_rows=15)
    rows = _acl_rows(html)
    assert len(rows) == 3
    expected = [("Sample Alliance", "Alliance"), ("Sample Friend", "Char"), ("Sample Corp", "Corp")]
    for row, (name, short) in zip(rows, expected):
        assert row["attrs"]["data-click"] == "toggleMRow"
        k1, k2 = row_keys(row)
        assert k1["text"] == name and "m-only" not in _classes(k1["attrs"])
        assert k2["text"] == short and "m-only" in _classes(k2["attrs"])
        # The desktop Type column (before Name in the DOM) stays untagged.
        first = row["cells"][0]
        assert "data-m" not in first["attrs"] and "data-m-label" not in first["attrs"]
        labelled = row_labelled(row)
        assert list(labelled) == ["Name", "Permission", "Remove"]
        assert labelled["Name"]["text"] == name
        assert_single_value_child(row)
        (select,) = labelled["Permission"]["kids"]
        assert select["data-change"] == "submitForm"
        assert "/permission" in labelled["Permission"]["attrs"]["action"]
        remove = labelled["Remove"]["attrs"]
        assert remove["data-confirm"] == f"Remove {name} from the ACL?"
        (button,) = labelled["Remove"]["kids"]
        assert "b-btn" in _classes(button)
        assert "m-tap" not in _classes(button), "m-tap's 40px would shrink the 44px button"


def test_acl_header_is_m_head_and_add_form_stacks():
    html = _render_detail(can_admin=True, visibility="custom", acl_entries=_ACL)
    tags = _tags(html)
    heads = [a for t, a in tags if "m-head" in _classes(a)]
    assert len(heads) == 1 and "grid-template-columns:90px 1fr 110px 60px" in heads[0]["style"]
    (add,) = [a for t, a in tags if t == "form" and a.get("action", "").endswith("/acl/add")]
    assert "m-stack" in _classes(add)


def test_promote_forms_stack_on_phones():
    html = _render_detail(can_admin=True, eligible_corps=[{"id": 98000001, "name": "Sample Corp"}],
                          eligible_alliances=[{"id": 99000001, "name": "Sample Alliance"}])
    tags = _tags(html)
    forms = [a for t, a in tags if t == "form" and a.get("action", "").endswith("/promote")]
    assert len(forms) == 2
    for f in forms:
        assert "m-stack" in _classes(f)
    # The scope bar stacks too, so each form spans the panel.
    (bar,) = [a for t, a in tags if "skp-scope" in _classes(a)]
    assert "m-stack" in _classes(bar)


# ── 3. Gap check (D15 A) ──────────────────────────────────────────────

def _gap_rows():
    """Rows as gap_analysis() builds them: one done, two pending."""
    return [
        {"skill_name": "Sample Gunnery Basics", "target_level": 5, "current_level": 5,
         "completed": True, "sp_needed": 0, "time_str": "done"},
        {"skill_name": "Sample Hull Upgrades", "target_level": 4, "current_level": 2,
         "completed": False, "sp_needed": 120000, "time_str": "1d 4h"},
        {"skill_name": "Sample Drone Operation", "target_level": 3, "current_level": 0,
         "completed": False, "sp_needed": 30000, "time_str": "9h 12m"},
    ]


def _injectors():
    return {"optimal": {"large": 2, "small": 1, "cost": 2450000000},
            "large_only": {"large": 3, "small": 0, "cost": 2700000000},
            "small_only": {"large": 0, "small": 9, "cost": 2900000000},
            "large_price": 900000000, "small_price": 320000000}


def _render_gap():
    return render_page(sp_mod, "partials/skill_plan_gap.html", "/skill-plans/42/gap/90000101",
                       rows=_gap_rows(), total_sp=150000, total_time="1d 13h",
                       completed=1, total=3, char=_CHARS[0], char_total_sp=48000000,
                       injectors=_injectors())


def test_gap_rows_lead_with_status_and_key_on_time():
    html = _render_gap()
    assert_mrow(html, min_rows=3)
    rows = cells_rows(html)
    assert len(rows) == 3
    # (key 1, key 2, the full Name line, Level)
    expected = [("✓ Sample Gunnery Basics V", "done", "Sample Gunnery Basics V", "5 → 5"),
                ("● Sample Hull Upgrades IV", "1d 4h", "Sample Hull Upgrades IV", "2 → 4"),
                ("● Sample Drone Operation III", "9h 12m", "Sample Drone Operation III", "0 → 3")]
    for row, (k1_text, k2_text, name, level) in zip(rows, expected):
        assert row["attrs"]["data-click"] == "toggleMRow"
        assert "skp-gap-row" in _classes(row["attrs"])
        k1, k2 = row_keys(row)
        assert k1["text"] == k1_text
        assert k2["text"] == k2_text
        assert {lbl: c["text"] for lbl, c in row_labelled(row).items()} == {"Name": name, "Level": level}
        assert_single_value_child(row)
        # Every cell is tagged: nothing is hidden on phones.
        assert all("data-m" in c["attrs"] or "data-m-label" in c["attrs"] for c in row["cells"])
    assert "opacity:0.5" in rows[0]["attrs"]["style"]
    assert "opacity" not in rows[1]["attrs"]["style"]


def test_gap_summary_and_injector_options_carry_wrap_hooks():
    tags = _tags(_render_gap())
    for hook in ("skp-gap-sum", "skp-gap-totals", "skp-gap-alt"):
        assert len([a for t, a in tags if hook in _classes(a)]) == 1, hook


# ── 5. Typeahead rows (route-built HTML) ──────────────────────────────

class _Req:
    def __init__(self, q):
        self.query_params = {"q": q}


class _ShipResult:
    def fetchall(self):
        return [_NS(type_id=601, type_name="Sample Frigate"),
                _NS(type_id=602, type_name='Sample "Quoted" Cruiser')]


class _ShipDB:
    async def execute(self, _stmt):
        return _ShipResult()


def _typeahead_rows(html):
    return [a for t, a in _tags(html) if t == "div"]


def test_skill_typeahead_rows_carry_the_phone_class(monkeypatch):
    async def fake_search(_db, _q, limit=10):
        return [{"type_id": 3301, "type_name": "Sample Gunnery Basics"},
                {"type_id": 3302, "type_name": "Sample <Odd> & Name"}]

    monkeypatch.setattr(sp_mod.sde, "search_skills", fake_search)
    resp = asyncio.run(sp_mod.search_skills_api(_Req("sample"), db=None))
    rows = _typeahead_rows(resp.body.decode())
    assert len(rows) == 2
    for r in rows:
        assert {"b-hover-border", "skp-ta-row"} <= set(_classes(r))
        assert r["data-click"] == "selectSkill"
    assert rows[1]["data-name"] == "Sample <Odd> & Name"


def test_ship_typeahead_rows_carry_the_phone_class():
    resp = asyncio.run(sp_mod.search_ships_api(_Req("sample"), db=_ShipDB()))
    rows = _typeahead_rows(resp.body.decode())
    assert len(rows) == 2
    for r in rows:
        assert {"b-hover-border", "skp-ta-row"} <= set(_classes(r))
        assert r["data-click"] == "selectShip"
    assert rows[1]["data-name"] == 'Sample "Quoted" Cruiser'


def test_typeahead_dropdowns_carry_the_phone_hook():
    tags = _tags(_render_detail(can_edit=True))
    for rid in ("skill-results", "ship-results"):
        (box,) = [a for t, a in tags if a.get("id") == rid]
        assert "skp-results" in _classes(box)
        # Desktop keeps the absolutely positioned dropdown.
        assert "position:absolute" in box["style"] and "max-height:200px" in box["style"]


# ── 6. Shared view ────────────────────────────────────────────────────

def _render_shared(**session):
    plan = _plan(share_token="sample-share-token")
    return render_page(sp_mod, "skill_plan_shared.html", "/skill-plans/shared/sample-share-token",
                       **session, plan=plan, entries=_entries(3), characters=_CHARS,
                       share_token="sample-share-token")


def test_shared_view_logged_in_gap_picker_wraps_and_rows_stay_plain():
    html = _render_shared()
    tags = _tags(html)
    (pick,) = [a for t, a in tags if "skp-gap-pick" in _classes(a)]
    assert "display:flex" in pick["style"]
    assert 'id="gap-char"' in html
    # Public page: its rows are not m-rows (no tap-to-open for anonymous).
    assert mrows(html) == []


def test_detail_gap_picker_wraps_like_the_shared_one():
    tags = _tags(_render_detail(can_edit=False))
    (pick,) = [a for t, a in tags if "skp-gap-pick" in _classes(a)]
    assert "display:flex" in pick["style"]


def test_shared_view_attribute_line_carries_its_phone_hook():
    """Each row's attributes and rank ("Mem/Per ×2") are 9px inline; the
    hook lets phones show them at 11px. The rank span inherits the size.
    Desktop keeps the inline size (D21). Logged in and anonymous alike."""
    for session in ({}, {"session": None}):
        html = _render_shared(**session)
        attrs = [a for t, a in _tags(html) if "skp-shared-attr" in _classes(a)]
        assert len(attrs) == 3
        for a in attrs:
            assert _classes(a) == ["skp-shared-attr"]
            assert "font-size:9px" in a["style"].replace(" ", "")
        spans = re.findall(r'<span class="skp-shared-attr"[^>]*>(.*?)</span>\s*</span>', html, re.S)
        assert len(spans) == 3 and all('class="skp-shared-rank"' in s for s in spans)


def test_shared_view_analyze_is_the_picker_rows_button():
    """Analyze is a .b-btn directly in the picker row, 9px inline."""
    html = _render_shared()
    (pick,) = re.findall(r'<div class="skp-gap-pick".*?</div>', html, re.S)
    (btn,) = re.findall(r'<button class="b-btn" data-click="runGap"\s+style="([^"]*)"', pick)
    assert "font-size:9px" in btn.replace(" ", "")


# ── CSS (the R2 T6 section) ───────────────────────────────────────────

def _decls(selector):
    body = rule_bodies(css_section("T6"), selector)
    assert body, f"no rule for {selector} in the R2 T6 section"
    return re.sub(r"\s+", " ", body)


def test_css_gap_summary_and_injector_lines_wrap():
    for sel in (".skp-gap-sum", ".skp-gap-totals", ".skp-gap-alt"):
        assert "flex-wrap: wrap" in _decls(sel), sel


def test_css_gap_picker_puts_the_select_on_its_own_line():
    assert "flex-wrap: wrap" in _decls(".skp-gap-pick")
    sel = _decls(".skp-gap-pick > select")
    assert "flex: 1 1 100% !important" in sel and "min-width: 0" in sel
    # A percentage width keeps a long character name from widening the
    # page column (min-width alone doesn't: found at 360px).
    assert "width: 100%" in sel


def test_share_row_carries_its_hook():
    html = render_page(
        sp_mod, "skill_plan_detail.html", "/skill-plans/42",
        plan=_plan(share_token="sample-share-token"), entries=_entries(3), characters=_CHARS,
        corp_names={}, alliance_names={}, can_edit=True, can_admin=True, is_owner=True,
        eligible_corps=[], eligible_alliances=[], acl_entries=[], acl_err=None)
    m = re.search(r'<div class="skp-share"[^>]*>(.*?)</div>', html, re.S)
    assert m, "the share-link row needs the skp-share hook"
    assert 'id="share-url"' in m.group(1) and 'data-click="copyShareUrl"' in m.group(1)


def test_css_share_link_gets_a_full_width_line_with_an_ellipsis():
    d = _decls(".skp-share > #share-url")
    assert "flex: 1 1 100% !important" in d and "min-width: 0" in d
    assert "white-space: nowrap" in d and "text-overflow: ellipsis" in d


def test_css_share_link_rule_is_scoped_to_this_page():
    """#share-url is reused by the intel, D-scan and image-share pages; a
    bare #share-url rule here would restyle their inputs too."""
    preludes = re.findall(r"([^{}]+)\{", css_section("T6"))
    for prelude in preludes:
        for sel in selectors(prelude):
            if "#share-url" in sel:
                assert sel.startswith(".skp-share "), f"unscoped selector {sel!r}"


def test_sort_toolbar_buttons_are_tap_targets():
    """With the drag handle hidden on phones, Export and the three Sort
    buttons are the only list controls: each keeps the 44px phone button
    height (no m-tap, whose 40px would shrink it); the CSS widens them."""
    tags = _tags(_render_detail(can_edit=True))
    i = next(n for n, (t, a) in enumerate(tags) if "skp-sortbar" in _classes(a))
    buttons = []
    for t, a in tags[i + 1:]:
        if t == "button":
            buttons.append(a)
        if len(buttons) == 4:
            break
    assert buttons[0].get("data-click") == "exportPlan"
    for b in buttons:
        assert "m-tap" not in _classes(b), b


def test_read_only_sort_toolbar_keeps_export_tappable():
    tags = _tags(_render_detail(can_edit=False))
    (export,) = [a for t, a in tags if a.get("data-click") == "exportPlan"]
    assert "m-tap" not in _classes(export)


def test_css_sort_toolbar_wraps_centred_at_12px():
    d = _decls(".skp-sortbar")
    assert "flex-wrap: wrap" in d and "align-items: center" in d
    assert "font-size: 12px !important" in d
    b = _decls(".skp-sortbar button")
    assert "min-width: 40px" in b and "font-size: 12px !important" in b
    assert "height" not in b, "the global phone button rule gives 44px"


def test_css_typeahead_rows_are_40px_12px_left_aligned():
    d = _decls(".skp-ta-row")
    assert "min-height: 40px" in d
    assert "font-size: 12px !important" in d
    assert "text-align: left" in d


def test_css_typeahead_dropdown_flows_in_the_panel():
    """A panel clips (overflow:hidden; on phones the safety net's
    overflow-x:auto), so on phones the dropdown sits in the flow instead of
    hanging over the controls below it."""
    assert "position: static !important" in _decls(".skp-results")


def test_css_acl_keys_are_readable():
    assert "font-size: 12px" in _decls('.skp-acl-row > [data-m="key"]')


def test_css_attributes_rank_is_legible_when_open():
    assert "color: var(--muted) !important" in _decls('.skill-row > [data-m-label="Attributes"] span span')


def test_css_remove_buttons_are_44px_squares_in_an_open_row():
    """.b-btn carries flex:1, which stretched the × across the open row
    (250px at 360) once its form became the open row's flex value. The
    global phone button rule gives the 44px height; this widens it."""
    for sel in (".skill-row > [data-m-label] > .b-btn", ".skp-acl-row > [data-m-label] > .b-btn"):
        d = _decls(sel)
        assert "flex: none" in d and "min-width: 44px" in d, sel
        assert "height" not in d, sel


def test_css_gap_row_keys_share_one_size():
    """Key 1's spans are 10–11px inline and key 2 is 10px; at the phone body
    size of 16px the two keys sat at different heights (r2probe: "keys on
    different lines")."""
    for sel in ('.skp-gap-row > [data-m="key"]', '.skp-gap-row > [data-m="key"] > *'):
        assert "font-size: 12px !important" in _decls(sel), sel


def _after_phone_block(section):
    """What follows the section's phone @media block, found by matching its
    braces from the block's own `{` (not by the last `}` in the section,
    which a trailing desktop rule would also end with)."""
    m = re.match(r"\s*@media \(max-width: 640px\) \{", section)
    assert m, "the section must open with its phone @media block"
    depth = 1
    for i in range(m.end(), len(section)):
        depth += (section[i] == "{") - (section[i] == "}")
        if depth == 0:
            return section[i + 1:]
    raise AssertionError("the phone @media block never closes")


def test_after_phone_block_sees_a_trailing_desktop_rule():
    """The guard below isn't blind: a rule after the block is reported."""
    sample = "\n@media (max-width: 640px) {\n    .a { color: red; }\n}\n.b { color: blue; }\n"
    assert _after_phone_block(sample).strip() == ".b { color: blue; }"
    assert _after_phone_block("@media (max-width: 640px) {\n    .a { b: c; }\n}\n").strip() == ""


def test_css_section_is_phone_only():
    """Desktop renders as before (D21): every T6 rule sits inside the one
    phone block, and only whitespace follows it up to the end marker."""
    rest = _after_phone_block(css_section("T6"))
    assert rest.strip() == "", f"the R2 T6 section has a rule outside its phone block: {rest.strip()[:80]!r}"


_SMALL_TEXT = (".skp-meta > span", ".skp-actions .b-btn", ".skp-scope .b-btn", ".skp-scope > span",
               ".skp-acl-count", ".skp-acl-add > .b-btn", ".skp-acl-hint", ".skp-share > span",
               ".skp-share > .b-btn", ".skp-tools .b-btn", "#ship-link", ".skp-hint")


def test_css_small_text_is_11px():
    for sel in _SMALL_TEXT:
        assert "font-size: 11px !important" in _decls(sel), sel


def test_css_shared_view_attribute_line_and_analyze_are_11px():
    """The shared view's attribute line and both pages' Analyze button: 11px
    over their inline 9px (the detail page's Analyze already was, through
    .skp-tools .b-btn; the shared page has no .skp-tools)."""
    assert "font-size: 11px !important" in _decls(".skp-shared-attr")
    assert "font-size: 11px !important" in _decls(".skp-gap-pick > .b-btn")
    assert 'class="b-grid-2 skp-tools"' not in _render_shared()


def test_small_text_hooks_are_in_the_markup():
    html = _render_detail(can_admin=True, can_edit=True, visibility="custom", acl_entries=_ACL)
    for cls in ("skp-meta", "skp-actions", "skp-acl-count", "skp-acl-hint", "skp-hint"):
        assert re.search(rf'class="{cls}"', html), cls
    assert 'class="m-stack skp-acl-add"' in html
    assert 'class="b-grid-2 skp-tools"' in html
