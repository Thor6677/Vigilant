"""Mobile R2 polish A: a full-name line in every opened row, shrinkable
labels, and a readable rank on the shared plan view.

1. Every opened row on the R2 lists below shows its full, untruncated name
   as a labelled "Name" line (user decision, 2026-10-04: "Every opened
   row"). It is an m-only cell with one plain-text value, the same text the
   desktop row shows, and the first labelled cell in DOM order, so it is the
   first line under the row. It is redundant when the name already fits;
   the user accepted that. Not included: link rows (Can fly now, which
   don't open), the mining Daily Summary (key 1 is a date), R1 lists and the
   public shared plan view (no m-rows).
2. R1's `.m-row > [data-m-label]::before` label may shrink (it wraps) instead
   of holding a long label at full width, where justify-content:flex-end
   pushed its start out of the row's left edge. That was patched locally
   for the missing-skill cards (.cf-miss); the rule is global now. A short
   label is alone on its line or fits beside its value, so it never
   shrinks: it renders as before.
3. The shared plan's ×rank is drawn in var(--border), which all but
   vanishes. On phones it takes var(--muted) through a hook class; desktop
   is unchanged (D21) and the public page gains no data-click.

Contexts follow the shapes the routes build. Names and ids are invented."""
import re
import types

from app.routes import character_detail as cd_mod
from app.routes import corporations as corps_mod
from app.routes import mining as mining_mod
from app.routes import skill_plans as sp_mod
from app.routes import skills as skills_mod
from tests._mobile import (SITE_CSS, Styled, assert_mrow, assert_single_value_child,
                           cells_rows, css_section, render_page, row_keys, row_labelled,
                           rule_bodies, selectors)

_NS = types.SimpleNamespace
_ROMAN = ["", "I", "II", "III", "IV", "V"]


def _classes(attrs):
    return attrs.get("class", "").split()


def _labelled(row):
    """A row's labelled cells in DOM order (row_labelled keys them by label)."""
    return [c for c in row["cells"] if "data-m-label" in c["attrs"]]


def _assert_name_lines(html, rows, names):
    """Each row's first labelled cell is an m-only "Name" span holding just
    the full name as plain text, and the row still meets the contract."""
    assert_mrow(html, min_rows=len(rows))
    assert rows and len(rows) == len(names), (len(rows), len(names))
    for row, name in zip(rows, names):
        cells = _labelled(row)
        assert cells, f"{name!r}: no labelled cells"
        first = cells[0]
        assert first["attrs"]["data-m-label"] == "Name", (
            f"{name!r}: first labelled cell is {first['attrs']['data-m-label']!r}")
        assert [c["attrs"]["data-m-label"] for c in cells].count("Name") == 1
        assert first["text"] == name
        assert first["tag"] == "span"
        assert "m-only" in _classes(first["attrs"]), "the Name line is phone-only"
        assert first["kids"] == [], "one plain-text value, no element children"
        assert "style" not in first["attrs"]
        assert_single_value_child(row)


# ── T1: missing-skill cards (partials/character_can_fly.html) ─────────

_SHIP = "Sample Long Hull Strategic Battlecruiser"
_FITS = ["Sample Extremely Long Fleet Doctrine Fit & Spare Name", "Short Fit"]


def _missing_fit(i, name):
    return {"id": 300 + i, "name": name, "ship_type_id": 99202, "ship_name": _SHIP,
            "folder": "Sample Doctrine" if i == 0 else None, "can_fly": False,
            "missing": [{"skill_id": 33101, "have": 0, "need": 3,
                         "skill_name": "Sample Advanced Capital Ship Construction Theory",
                         "time_str": "1h 2m"}],
            "missing_training_minutes": 62.0, "missing_training_str": "1h 2m"}


def _render_can_fly():
    fly = [{"id": 1, "name": "Sample Fit", "ship_type_id": 99201, "ship_name": "Sample Frigate",
            "folder": None, "can_fly": True, "missing": []}]
    miss = [_missing_fit(i, n) for i, n in enumerate(_FITS)]
    return render_page(cd_mod, "partials/character_can_fly.html", "/character/90000001/can-fly",
                       error=None, no_scope=False, total=len(fly) + len(miss), can_fly=len(fly),
                       can_fly_fits=fly, missing_fits=miss)


def test_missing_skill_cards_open_with_the_ship_and_fit_name():
    html = _render_can_fly()
    rows = [r for r in cells_rows(html) if "cf-miss" in _classes(r["attrs"])]
    _assert_name_lines(html, rows, [f"{_SHIP} — {n}" for n in _FITS])
    # Plain text, escaped once: the same string the desktop head row shows.
    assert html.count("Fit &amp; Spare Name") == 3      # desktop head, key 1, Name


def test_can_fly_now_link_rows_get_no_name_line():
    """Link rows don't open, so they carry no labelled cells at all."""
    rows = [r for r in cells_rows(_render_can_fly()) if "m-row--link" in _classes(r["attrs"])]
    assert len(rows) == 1
    assert not row_labelled(rows[0])


# ── T1: corporation history (character_detail.html) ───────────────────

_CORPS = ["Sample Interstellar Logistics & Deep Space Salvage Consortium", "Sample Corp Short"]


def _render_overview():
    history = [{"corporation_id": 98000001 + i, "corporation_name": n, "start_date": "2025-06-01",
                "days_in": 490 if i == 0 else None, "is_current": i == 0}
               for i, n in enumerate(_CORPS)]
    pilot = _NS(character_id=90000001, character_name="Pilot Alpha", corporation_name="Sample Corp",
                alliance_name=None, security_status=1.5, birthday=None)
    return render_page(
        cd_mod, "character_detail.html", "/character/90000001",
        char=pilot, killmails_enabled=False, current_wallet=1.0e9,
        journal=[], journal_error=None, chart_data_json='{"labels": [], "values": []}',
        active_range="1m", ranges=["1d", "1w", "1m"],
        active_skill={"skill_name": "Sample Skill", "finished_level": 3, "remaining_seconds": 3600},
        skillqueue=[], completed_skills=[], corp_history=history,
        total_sp_in_queue=0, total_trained_sp=5000000, unallocated_sp=0,
        has_implants_scope=False, last_synced_str="5m ago", queue_remaining=3600,
        zkill=[], kills=0, losses=0, has_assets_scope=True, docked_at=None,
        current_system=None, implants=[], jump_clones=[])


def test_corp_history_rows_open_with_the_corporation_name():
    html = _render_overview()
    rows = [r for r in cells_rows(html) if "ov-corp" in _classes(r["attrs"])]
    _assert_name_lines(html, rows, _CORPS)
    for row in rows:
        assert [c["attrs"]["data-m-label"] for c in _labelled(row)] == ["Name", "Joined"]


# ── T2: Skill Queue (skills.html) and remap comparison ────────────────

_LONG_SKILL = "Sample Heavy Assault Missile Launcher Specialization"
_QUEUE = [(_LONG_SKILL, 5), ("Sample Skill Two", 2)]


def _render_skills():
    names = skills_mod.ATTR_NAMES
    queue = [{"skill_id": 3300 + i, "name": n, "level": lvl, "rank": 8, "primary": 3, "secondary": 4,
              "primary_name": names[3], "secondary_name": names[4], "sp_needed": 123456,
              "time_str": "12d 3h 4m", "time_minutes": 17464.0}
             for i, (n, lvl) in enumerate(_QUEUE)]
    return render_page(
        skills_mod, "skills.html", "/character/90000001/skills",
        char={"character_id": 90000001, "character_name": "Sample Pilot",
              "corporation_name": "Sample Corp", "scopes": "esi-skills.read_skills.v1"},
        error=None, attributes=[17, 27, 21, 17, 17], attr_names=names,
        attr_keys=skills_mod.ATTR_KEYS, implants=[0, 0, 0, 0, 0], total_sp=12345678,
        queue_items=queue, total_current_minutes=57720.0, current_time_str="40d 2h 0m",
        optimal_attrs=[17, 27, 17, 21, 17], optimal_time=54840.0,
        optimal_time_str="38d 2h 0m", time_saved=2880.0, time_saved_str="2d 0h 0m",
        bonus_remaps=1, last_remap="", next_remap="2026-11-01")


def _skills_key_rows(html):
    return [r for r in cells_rows(html)
            if row_keys(r) and "skills-key" in _classes(row_keys(r)[0]["attrs"])]


def test_skill_queue_rows_open_with_skill_and_level():
    html = _render_skills()
    rows = _skills_key_rows(html)
    _assert_name_lines(html, rows, [f"{n} {_ROMAN[lvl]}" for n, lvl in _QUEUE])
    for row in rows:
        assert [c["attrs"]["data-m-label"] for c in _labelled(row)] == ["Name", "Attributes", "SP"]


def _render_remap():
    names = skills_mod.ATTR_NAMES
    rows = [{"name": n, "level": lvl, "primary_name": names[1], "secondary_name": names[2],
             "current_time": "3d 4h 0m", "proposed_time": "2d 1h 0m", "diff_minutes": 1620.0,
             "diff_str": "1d 3h 0m", "faster": True} for n, lvl in _QUEUE]
    return render_page(
        skills_mod, "partials/remap_results.html", "/character/90000001/skills/remap-calc",
        rows=rows, proposed=[17, 27, 17, 21, 17], total_points=99, valid=True,
        current_total_str="40d 2h 0m", proposed_total_str="27d 22h 56m", time_diff=1620.0,
        time_diff_str="1d 3h 0m", is_faster=True, attr_names=names)


def test_remap_rows_open_with_skill_and_level():
    html = _render_remap()
    rows = _skills_key_rows(html)
    _assert_name_lines(html, rows, [f"{n} {_ROMAN[lvl]}" for n, lvl in _QUEUE])
    for row in rows:
        assert [c["attrs"]["data-m-label"] for c in _labelled(row)] == [
            "Name", "Attributes", "Current", "Remapped"]


# ── T4: mining (mining.html) ──────────────────────────────────────────

_ORES = {1001: "Sample Compressed Magnificent Glistening Bezdnacine Ore", 1002: "Sample Ore B"}
_SYSTEMS = {30000001: "Sample Extremely Long Wormhole System Designation", 30000002: "Sample Two"}


def _render_mining():
    raw = [{"date": f"2026-09-{d + 1:02d}", "type_id": 1001 + d % 2,
            "solar_system_id": 30000001 + d % 2, "quantity": 25000 + 1500 * d} for d in range(4)]
    data = mining_mod._aggregate_ledger(raw, _ORES, _SYSTEMS, {1001: 300.0, 1002: 100.0})
    html = render_page(mining_mod, "mining.html", "/character/90000001/mining",
                       char={"character_id": 90000001, "character_name": "Sample Miner",
                             "corporation_id": 98000001, "corporation_name": "Sample Mining Corp"},
                       data=data, error=None, is_corp=False, corp_id=None, characters=[])
    rows = cells_rows(html)
    sizes = [("ore", len(data["by_ore"])), ("system", len(data["by_system"])),
             ("daily", len(data["by_date"])), ("detail", len(data["entries"]))]
    assert len(rows) == sum(n for _, n in sizes)
    lists, at = {}, 0
    for name, n in sizes:
        lists[name], at = rows[at:at + n], at + n
    return html, data, lists


def test_mining_by_ore_rows_open_with_the_ore_name():
    html, data, lists = _render_mining()
    _assert_name_lines(html, lists["ore"], [o["name"] for o in data["by_ore"]])
    assert set(o["name"] for o in data["by_ore"]) == set(_ORES.values())


def test_mining_by_system_rows_open_with_the_system_name():
    html, data, lists = _render_mining()
    _assert_name_lines(html, lists["system"], [s["name"] for s in data["by_system"]])
    assert set(s["name"] for s in data["by_system"]) == set(_SYSTEMS.values())


def test_mining_full_detail_rows_open_with_the_ore_name_before_the_date():
    html, data, lists = _render_mining()
    _assert_name_lines(html, lists["detail"], [e["ore_name"] for e in data["entries"]])
    for row in lists["detail"]:
        assert [c["attrs"]["data-m-label"] for c in _labelled(row)] == [
            "Name", "Date", "System", "Quantity"]
        # Name is now the row's first child; the Date cell follows it.
        assert row["cells"][0]["attrs"].get("data-m-label") == "Name"


def test_mining_daily_summary_gets_no_name_line():
    """Its key 1 is a date, not a name."""
    _, _, lists = _render_mining()
    assert lists["daily"]
    for row in lists["daily"]:
        assert "Name" not in row_labelled(row)


# ── T5: corp detail structures and industry jobs ──────────────────────

_STRUCT_NAMES = ["Sample System IV - Sample Extremely Long Fortizar & Market Hub Name", "Sample Short"]
_PRODUCTS = ["Sample Capital Construction Parts And Components Blueprint Copy", None]


def _struct(name, state):
    return {"name": name, "type_name": "Sample Citadel", "system_name": "Sample System",
            "region": "Sample Region", "state": state,
            "state_class": corps_mod.STRUCTURE_STATE_CLASS.get(state, "is-warn"),
            "fuel_remaining": "12d 4h", "fuel_expires": "2026-10-20", "services": [],
            "reinforce_hour": None, "state_timer_end": None}


def _render_corp_detail():
    scopes = " ".join(corps_mod.CORP_SCOPES.values())
    jobs = [{"activity_name": corps_mod.ACTIVITY_NAMES[1], "product_name": p, "runs": 10,
             "time_remaining": "1d 3h", "installer_id": 90000001} for p in _PRODUCTS]
    return render_page(
        corps_mod, "partials/corp_detail.html", "/corporations/98000001/detail",
        corp_id=98000001,
        corp_info={"ticker": "SMPL", "member_count": 42, "tax_rate": 0.1, "alliance_id": None,
                   "war_eligible": False, "date_founded": "2020-01-01T00:00:00Z"},
        corp_chars=[_NS(character_id=90000001, character_name="Pilot Alpha",
                        alliance_name=None, scopes=scopes, declined_scopes="")],
        ceo_name="Pilot Alpha", member_count=42, corp_wallets=None, corp_wallet_total=None,
        wallet_history=None, corp_jobs=jobs, corp_orders=None,
        corp_structures=[_struct(_STRUCT_NAMES[0], "armor_reinforce"),
                         _struct(_STRUCT_NAMES[1], "shield_vulnerable")],
        corp_contracts=None, inv_alert_count=0, corp_roles={})


def test_corp_structure_rows_open_with_the_structure_name():
    html = _render_corp_detail()
    rows = [r for r in cells_rows(html) if "State" in row_labelled(r)]
    _assert_name_lines(html, rows, _STRUCT_NAMES)
    # Key 1 also carries the state badge; the Name line is the name alone.
    assert "ARMOR REINFORCE" in row_keys(rows[0])[0]["text"]


def test_corp_job_rows_open_with_the_product():
    html = _render_corp_detail()
    rows = [r for r in cells_rows(html) if "Activity" in row_labelled(r)]
    names = [p or "—" for p in _PRODUCTS]
    _assert_name_lines(html, rows, names)
    assert [row_keys(r)[0]["text"] for r in rows] == names, "the same text key 1 shows"


# ── T6: skill plan rows, ACL rows and gap rows ────────────────────────

_PLAN_SKILLS = [("Sample Very Long Capital Ship Construction Skill Name", 5), ("Sample Short", 2)]
_ACL_NAMES = ["Sample Alliance Of Very Long Names & Even Longer Tickers", "Sample Friend"]


def _render_plan(can_edit=True):
    entries = [{"id": 500 + i, "skill_type_id": 3300 + i, "skill_name": n, "target_level": lvl,
                "rank": 2.0, "primary_attr_name": "Intelligence", "secondary_attr_name": "Memory"}
               for i, (n, lvl) in enumerate(_PLAN_SKILLS)]
    acl = [_NS(id=1 + i, subject_type="alliance" if i == 0 else "character",
               subject_id=99000001 + i, subject_name=n, permission="view")
           for i, n in enumerate(_ACL_NAMES)]
    plan = _NS(id=42, name="Sample Fleet Plan", visibility="custom", owner_corp_id=None,
               owner_alliance_id=None, share_token=None, description="")
    return render_page(
        sp_mod, "skill_plan_detail.html", "/skill-plans/42",
        plan=plan, entries=entries, characters=[], corp_names={}, alliance_names={},
        can_edit=can_edit, can_admin=True, is_owner=True, eligible_corps=[],
        eligible_alliances=[], acl_entries=acl, acl_err=None)


def test_plan_skill_rows_open_with_the_skill_name():
    for can_edit in (True, False):
        html = _render_plan(can_edit)
        rows = [r for r in cells_rows(html) if "skill-row" in _classes(r["attrs"])]
        # Key 2 is the level, so the Name line is the skill name alone.
        _assert_name_lines(html, rows, [n for n, _ in _PLAN_SKILLS])
        for row, (n, lvl) in zip(rows, _PLAN_SKILLS):
            assert [k["text"] for k in row_keys(row)] == [n, _ROMAN[lvl]]


def test_acl_rows_open_with_the_name():
    html = _render_plan()
    rows = [r for r in cells_rows(html) if "skp-acl-row" in _classes(r["attrs"])]
    _assert_name_lines(html, rows, _ACL_NAMES)
    for row in rows:
        assert [c["attrs"]["data-m-label"] for c in _labelled(row)] == ["Name", "Permission", "Remove"]


def test_gap_rows_open_with_skill_and_level():
    rows = [{"skill_name": n, "target_level": lvl, "current_level": 1, "completed": False,
             "sp_needed": 1000, "time_str": "1h"} for n, lvl in _PLAN_SKILLS]
    html = render_page(
        sp_mod, "partials/skill_plan_gap.html", "/skill-plans/42/gap/90000101",
        rows=rows, total_sp=2000, total_time="2h", completed=0, total=2,
        char=_NS(character_id=90000101, character_name="Sample Pilot One"), char_total_sp=48000000,
        injectors={"optimal": {"large": 0, "small": 1, "cost": 1}, "large_only": {"large": 0, "small": 0, "cost": 0},
                   "small_only": {"large": 0, "small": 0, "cost": 0}, "large_price": 0, "small_price": 0})
    _assert_name_lines(html, cells_rows(html), [f"{n} {_ROMAN[lvl]}" for n, lvl in _PLAN_SKILLS])


# ── 2. The R1 label may shrink ────────────────────────────────────────

_R1_LABEL = ".m-row > [data-m-label]::before"


def _css():
    with open(SITE_CSS, encoding="utf-8") as fh:
        return re.sub(r"/\*.*?\*/", "", fh.read(), flags=re.S)


def _rules(css, selector):
    """Bodies of every innermost rule whose selector list holds `selector`."""
    return [m.group(2) for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css)
            if selector in selectors(m.group(1))]


def test_r1_label_rule_lets_a_long_label_shrink():
    (body,) = _rules(_css(), _R1_LABEL)
    body = re.sub(r"\s+", " ", body)
    assert "flex: 0 1 auto" in body and "min-width: 0" in body
    assert "flex: none" not in body


def test_r1_label_rule_keeps_its_other_declarations():
    """Only the flex sizing changed: a short label renders as before."""
    body = re.sub(r"\s+", " ", rule_bodies(_css(), _R1_LABEL))
    for decl in ("content: attr(data-m-label)", "text-align: left", "margin-right: auto",
                 "padding-right: 0.6rem", "color: var(--muted)", "font-size: 11px",
                 "letter-spacing: 0.06em", "text-transform: uppercase"):
        assert decl in body, decl


def test_no_section_keeps_a_local_label_shrink():
    """The missing-skill cards' local copy is gone: the global rule covers
    them, and no R2 section re-sizes the labels."""
    css = _css()
    assert not re.search(r"\.cf-miss\b", css)
    for task in ("T1", "T2", "T3", "T4", "T5", "T6"):
        for m in re.finditer(r"([^{}]+)\{", css_section(task)):
            for sel in selectors(m.group(1)):
                assert not ("data-m-label" in sel and "::before" in sel), (task, sel)


# ── 3. Shared plan: the rank is readable on phones ────────────────────

def _render_shared(**session):
    entries = [{"id": 1 + i, "skill_type_id": 3300 + i, "skill_name": f"Sample Skill {i}",
                "target_level": 3, "rank": r, "primary_attr_name": "Memory",
                "secondary_attr_name": "Perception"} for i, r in enumerate((1.0, 2.5))]
    plan = _NS(id=7, name="Sample Doctrine Plan", share_token="sample-share-token",
               visibility="personal", description="")
    return render_page(sp_mod, "skill_plan_shared.html", "/skill-plans/shared/sample-share-token",
                       **session, plan=plan, entries=entries, characters=[],
                       share_token="sample-share-token")


def _rank_spans(html):
    p = Styled()
    p.feed(html)
    p.close()
    return [(tag, cls, style) for tag, cls, style in p.tags if "skp-shared-rank" in cls]


def test_shared_plan_rank_carries_its_phone_hook():
    for session in ({"session": None}, {}):
        html = _render_shared(**session)
        spans = _rank_spans(html)
        assert len(spans) == 2
        for tag, cls, style in spans:
            assert tag == "span" and cls == ["skp-shared-rank"]
            # Desktop keeps its inline colour (D21).
            assert style.replace(" ", "") == "color:var(--border);"
        assert re.findall(r'<span class="skp-shared-rank"[^>]*>([^<]*)</span>', html) == [
            "&times;1", "&times;2.5"]
    # Public page: the hook is CSS only (no tap binding for a stranger).
    assert "data-click" not in _render_shared(session=None)


def test_css_shared_plan_rank_is_muted_on_phones_only():
    sec = css_section("T6")
    body = rule_bodies(sec, ".skp-shared-rank")
    assert "color: var(--muted) !important" in body, "!important beats the inline colour"
    # Phone-only: every mention of the hook in site.css is inside T6's
    # section, whose rules all sit in its one phone block (that section's
    # own tests check the block).
    assert len(re.findall(r"\.skp-shared-rank\b", _css())) == len(re.findall(r"\.skp-shared-rank\b", sec))
