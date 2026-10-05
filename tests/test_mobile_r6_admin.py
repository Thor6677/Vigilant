"""Mobile R6 T6: the Admin console at phone width.

- The eight section buttons become a "Section ▾" dropdown of the same
  switchTab buttons; the desktop strip stays outside #admin-content.
- Lists that re-render themselves every few seconds (ESI logs, the
  scheduler's sync table, the updater's run history) become two-line rows
  through phone CSS alone (D15 A): no toggleMRow, no JS state to lose.
- Users keep their togglePanel header; on phones the role select and
  Delete move to the top of the opened user, as copies of the originals
  (D16 A).
- The audit log, which never auto-refreshes, becomes tap-to-open m-rows:
  event · character · age, with the full detail inside (D17 A).

Contexts follow the shapes app/routes/admin.py builds; every name and id
is invented."""
import functools
import re
import types
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path

import pytest

from app.routes import admin as admin_mod
from tests._mobile import (VOID, assert_mrow, assert_single_value_child,
                           cells_rows, css_section, norm, phone_block,
                           render_page, row_keys, row_labelled, row_lead,
                           rule_bodies, selectors, source)

_section = functools.partial(css_section, release="R6")
_NS = types.SimpleNamespace
NOW = datetime(2030, 1, 1, 12, 0, 0)
_ADMIN_SESSION = {"user_id": 1, "is_admin": True,
                  "active_character_id": 90000001, "csrf_token": "t"}


def _age(dt):
    """A deterministic stand-in for admin._format_age, relative to NOW."""
    if not dt:
        return "Never"
    mins = int((NOW - dt).total_seconds() // 60)
    return f"{mins}m ago" if mins < 60 else f"{mins // 60}h ago"


# ── A small DOM, enough to walk parents and siblings ──────────────────

class _Node:
    def __init__(self, tag, attrs, parent):
        self.tag, self.attrs, self.parent = tag, attrs, parent
        self.children, self.text = [], ""

    @property
    def classes(self):
        return self.attrs.get("class", "").split()

    def all_text(self):
        return norm(self.text + " ".join(c.all_text() for c in self.children))

    def walk(self):
        for c in self.children:
            yield c
            yield from c.walk()

    def find_all(self, pred):
        return [n for n in self.walk() if pred(n)]

    def next_element(self):
        sibs = self.parent.children
        i = sibs.index(self)
        return sibs[i + 1] if i + 1 < len(sibs) else None


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
    p = _Tree()
    p.feed(html)
    p.close()
    return p.root


def _by_id(root, ident):
    found = root.find_all(lambda n: n.attrs.get("id") == ident)
    assert len(found) == 1, (ident, len(found))
    return found[0]


class _Balance(HTMLParser):
    """Every end tag must close the innermost open element."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.problems = [], []

    def handle_starttag(self, tag, attrs):
        if tag not in VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if self.stack and self.stack[-1] == tag:
            self.stack.pop()
        else:
            self.problems.append((tag, self.getpos(), list(self.stack[-3:])))


# ── admin.html: the Section dropdown ──────────────────────────────────

def _admin(initial="users"):
    return render_page(admin_mod, "admin.html", "/admin", session=dict(_ADMIN_SESSION),
                       sections=admin_mod.ADMIN_SECTIONS, initial_section=initial)


def test_dropdown_lists_every_section_as_a_switch_tab_button():
    root = _tree(_admin("esi"))
    dd = _by_id(root, "admin-tabs-m")
    assert dd.tag == "details"
    assert {"m-tabs", "m-only"} <= set(dd.classes)
    buttons = dd.find_all(lambda n: n.tag == "button")
    assert [(b.attrs.get("data-section"), b.all_text()) for b in buttons] == list(admin_mod.ADMIN_SECTIONS)
    for b in buttons:
        assert b.attrs.get("data-click") == "switchTab"
        assert b.attrs.get("type") == "button"
    assert [b.attrs["data-section"] for b in buttons if "is-active" in b.classes] == ["esi"]
    summary = dd.find_all(lambda n: n.tag == "summary")[0]
    current = summary.find_all(lambda n: "m-tabs-current" in n.classes)
    label = summary.find_all(lambda n: "m-tabs-label" in n.classes)
    assert current and current[0].all_text() == "ESI Health"
    assert label and label[0].all_text() == "Section"


def test_desktop_strip_is_marked_and_both_navs_stay_outside_admin_content():
    html = _admin()
    root = _tree(html)
    strip = _by_id(root, "admin-tabs")
    assert strip.classes == ["b-tab-strip", "m-tabs-desktop"]
    assert [b.attrs.get("data-section") for b in strip.find_all(lambda n: n.tag == "button")] == \
        [k for k, _ in admin_mod.ADMIN_SECTIONS]
    content = _by_id(root, "admin-content")
    assert not content.find_all(lambda n: n.attrs.get("id") in ("admin-tabs", "admin-tabs-m"))
    assert html.index('id="admin-tabs"') < html.index('id="admin-tabs-m"') < html.index('id="admin-content"')


def test_admin_content_keeps_its_swap_and_error_opt_out():
    content = _by_id(_tree(_admin()), "admin-content")
    assert content.attrs.get("hx-swap") == "innerHTML"
    assert content.attrs.get("data-htmx-no-error") == "1"
    assert content.attrs.get("hx-get") == "/admin/section/users"


def _switch_tab_body():
    src = source("admin.html")
    fn = src[src.index("function switchTab()"):]
    return fn[:fn.index("\n}")]


def test_switch_tab_syncs_both_button_sets_and_closes_the_dropdown():
    fn = _switch_tab_body()
    assert "'#admin-tabs button, #admin-tabs-m button'" in fn
    # Active state follows the section, so whichever set was tapped, both agree.
    assert "dataset.section === section" in fn
    assert ".m-tabs-current" in fn
    assert ".open = false" in fn
    # Everything the strip did before is still done, in the same call.
    assert "htmx.ajax('GET', '/admin/section/' + section, '#admin-content')" in fn
    assert "history.replaceState" in fn
    assert "setInterval(refreshAdminSection, interval)" in fn


def test_dropdown_adds_no_refresh_timer():
    """test_updater_panel pins exactly two setInterval sites; the dropdown
    reuses switchTab rather than adding its own."""
    src = source("admin.html")
    assert src.count("setInterval(") == 2


# ── Refreshing lists: ESI, Scheduler, Updates history (D15 A) ─────────

def _esi(**over):
    t0 = datetime(2029, 12, 31, 18, 42, 7)
    log = [_NS(timestamp=t0, status_code=200, group="char-detail",
               path="/characters/90000001/skills/"),
           _NS(timestamp=t0 - timedelta(seconds=2), status_code=429, group=None,
               path="/markets/10000002/orders/?order_type=all&page=3")]
    ev = lambda i, kind, retry, archived=None: _NS(
        id=i, event_type=kind, group_name="market" if i % 2 else None,
        path=f"/markets/1000000{i}/history/", retry_after=retry,
        occurred_at=t0 - timedelta(hours=i), archived_at=archived)
    ctx = dict(
        etag_cache={"entries": 10, "max_entries": 500, "utilization_pct": 2},
        db_cache={"active_entries": 5, "expired_entries": 1, "total_entries": 6},
        esi_status="ok", total_requests=12, ok_count=10, cached_count=1,
        rejected_count=1, ok_rate=90, groups=[], legacy=None, request_log=log,
        recent_events=[ev(1, "429", 30), ev(2, "420", None)],
        archived_events=[ev(3, "429", 5, t0), ev(4, "group_warning", None, t0)],
        chart_data_json='{"labels": []}')
    ctx.update(over)
    return render_page(admin_mod, "partials/admin_esi.html", "/admin/section/esi", **ctx)


def _scheduler(**over):
    rows = [dict(character_id=90000001 + i, character_name=name, last_synced_str=f"{i + 1}m ago",
                 sync_status=status, queued=queued, stale_fields=stale, total_fields=12,
                 sync_warnings={}, warn_count=warns, sync_error=err)
            for i, (name, status, queued, stale, warns, err) in enumerate([
                ("Sample Pilot Alpha", "idle", False, 0, 0, None),
                ("Sample Pilot Bravo With A Long Name", "error", False, 12, 0, "token refresh failed: invalid_grant"),
                ("Sample Pilot Charlie", "syncing", False, 3, 2, None),
                ("Sample Pilot Delta", "idle", True, 1, 0, None)])]
    ctx = dict(queue_depth=1, active_syncs=1, sync_concurrency=4, semaphore_available=3,
               last_inv_check="2m ago", notification_queues=0, stuck_characters=[],
               char_sync_rows=rows)
    ctx.update(over)
    return render_page(admin_mod, "partials/admin_scheduler.html", "/admin/section/scheduler", **ctx)


@pytest.mark.parametrize("name", ["partials/admin_esi.html", "partials/admin_scheduler.html",
                                  "partials/updater_panel.html", "partials/admin_updates.html"])
def test_refreshing_sections_carry_no_tap_to_open_rows(name):
    """Their bodies are replaced every 2-10s: an opened row would snap shut."""
    src = re.sub(r"\{#.*?#\}", "", source(name), flags=re.S)   # comments may explain why
    assert "toggleMRow" not in src
    assert "m-row" not in src


@pytest.mark.parametrize("render", [_esi, _scheduler], ids=["esi", "scheduler"])
def test_rendered_refreshing_sections_carry_no_tap_to_open_rows(render):
    html = render()
    assert "toggleMRow" not in html
    assert not cells_rows(html)


def _two_line_rows(html):
    return _tree(html).find_all(lambda n: "adm-2l" in n.classes)


def _hooks(row):
    """The direct children's two-line roles, in DOM order."""
    roles = []
    for c in row.children:
        role = [k for k in c.classes if k.startswith("adm-2l-")]
        roles.append(role[0] if role else None)
    return roles


def test_esi_lists_are_two_line_rows():
    html = _esi()
    rows = _two_line_rows(html)
    # 2 requests + 2 active events + 2 archived events.
    assert len(rows) == 6
    for row in rows:
        roles = _hooks(row)
        for one in ("adm-2l-a", "adm-2l-main", "adm-2l-time"):
            assert roles.count(one) == 1, (one, roles)
        assert "adm-2l-sub" in roles
    # Line 1 is code/badge · path · time; the path is the main text.
    req = rows[0]
    by_role = {r: c for r, c in zip(_hooks(req), req.children) if r}
    assert by_role["adm-2l-a"].all_text() == "200"
    assert by_role["adm-2l-main"].all_text() == "/characters/90000001/skills/"
    assert by_role["adm-2l-time"].all_text() == "2029-12-31 18:42:07"
    assert by_role["adm-2l-sub"].all_text() == "char-detail"


def test_esi_header_rows_are_hidden_on_phones():
    root = _tree(_esi())
    heads = root.find_all(lambda n: "m-head" in n.classes)
    assert len(heads) == 3
    assert all("b-table-row" in h.classes for h in heads)


def test_esi_dismiss_controls_are_unchanged():
    html = _esi()
    root = _tree(html)
    dismiss = root.find_all(lambda n: n.tag == "button" and n.attrs.get("hx-post", "").endswith("/dismiss"))
    assert [b.attrs["hx-post"] for b in dismiss] == ["/admin/esi/events/1/dismiss", "/admin/esi/events/2/dismiss"]
    for b in dismiss:
        assert b.attrs.get("hx-target") == "#admin-content" and b.attrs.get("hx-swap") == "innerHTML"
        cell = b.parent
        assert {"adm-2l-sub", "adm-2l-act"} <= set(cell.classes)
    everything = root.find_all(lambda n: n.attrs.get("hx-post") == "/admin/esi/events/dismiss-all")
    assert everything and "hx-confirm" in everything[0].attrs


def test_esi_chart_and_archive_keep_their_hooks():
    root = _tree(_esi())
    canvas = _by_id(root, "admin-request-chart")
    assert canvas.attrs.get("data-chart") == '{"labels": []}'
    archive = _by_id(root, "esi-archived")
    assert archive.tag == "details" and "data-keep-open" in archive.attrs


def test_esi_partial_closes_every_tag():
    """The inventory reported an unclosed retry <span>; in this tree it is
    closed. Pin it so the two-line layout's cells stay siblings."""
    p = _Balance()
    p.feed(_esi())
    p.close()
    assert not p.problems and not p.stack, (p.problems, p.stack)


def test_scheduler_sync_table_is_two_line_rows():
    html = _scheduler()
    rows = _two_line_rows(html)
    assert len(rows) == 4
    for row in rows:
        roles = _hooks(row)
        assert roles.count("adm-2l-a") == 1 and roles.count("adm-2l-main") == 1
        assert roles.count("adm-2l-time") == 1 and roles.count("adm-2l-sub") == 2
    first = {r: c for r, c in zip(_hooks(rows[0]), rows[0].children) if r}
    assert first["adm-2l-main"].all_text() == "Sample Pilot Alpha"
    assert first["adm-2l-time"].all_text() == "1m ago"
    stale = [c for c in rows[1].children if "adm-stale" in c.classes]
    assert stale and stale[0].all_text() == "12/12"
    heads = _tree(html).find_all(lambda n: "m-head" in n.classes)
    assert len(heads) == 1


def test_scheduler_and_esi_buttons_keep_their_actions():
    root = _tree(_scheduler())
    sync_all = root.find_all(lambda n: n.attrs.get("hx-post") == "/admin/action/sync-all")
    assert sync_all and sync_all[0].attrs.get("hx-confirm") == "Sync all characters now?"


def test_updater_history_columns_are_the_ones_the_phone_css_places():
    """The phone history rows place cells by position (td:nth-child), so the
    column order is pinned here: a reorder must update site.css too."""
    src = source("partials/updater_panel.html")
    thead = re.search(r"<thead>\s*<tr>(.*?)</tr>", src, re.S).group(1)
    assert re.findall(r"<th>(.*?)</th>", thead) == ["When (UTC)", "Kind", "Change", "Outcome", "Delivered"]
    css = phone_block(_section("T6"))[0]
    assert "td:nth-child(4)" in css and "td:nth-child(3)" in css and "td:nth-child(1)" in css


# ── Users (D16 A) ─────────────────────────────────────────────────────

def _users(admin_role="admin"):
    me = _NS(id=1, role="admin", last_login=NOW - timedelta(minutes=5))
    other = _NS(id=2, role="manager", last_login=NOW - timedelta(hours=3))
    third = _NS(id=3, role="admin", last_login=NOW - timedelta(hours=9))
    char = lambda cid, name, corp="Sample Corp", tok="ok", sync="idle": dict(
        character_id=cid, character_name=name, corporation_name=corp, token_status=tok,
        sync_status=sync, last_synced=NOW - timedelta(minutes=7), scope_count=10)
    users = [
        dict(user=me, char_count=1, characters=[char(90000001, "Sample Pilot Alpha")],
             display_name="Sample Pilot Alpha", main_character_id=90000001),
        dict(user=other, char_count=2, characters=[
            char(90000002, "Sample Pilot Bravo"),
            char(90000003, "Sample Pilot Charlie With A Long Name", "Sample Corporation Of Length", "expired", "error")],
             display_name="Sample Pilot Bravo", main_character_id=90000002),
        dict(user=third, char_count=0, characters=[], display_name="User 3", main_character_id=None),
    ]
    allow = [_NS(id=7, entry_type="corporation", eve_id=98000001, name="Sample Corp"),
             _NS(id=8, entry_type="character", eve_id=90000009, name=None)]
    return render_page(admin_mod, "partials/admin_users.html", "/admin/section/users",
                       users=users, allowlist=allow, allowlist_enabled=True,
                       format_age=_age, admin_id=1, admin_role=admin_role)


def _user_blocks(root):
    heads = root.find_all(lambda n: n.attrs.get("data-click") == "togglePanel")
    return [(h, h.next_element()) for h in heads]


def test_user_header_rows_keep_toggle_panel_and_their_panel_follows():
    blocks = _user_blocks(_tree(_users()))
    assert len(blocks) == 3
    for head, panel in blocks:
        assert head.attrs.get("data-toggle-arrow") == ".expand-arrow"
        assert "adm-user" in head.classes
        # togglePanel opens this.nextElementSibling via inline display.
        assert panel.tag == "div" and "display:none" in panel.attrs.get("style", "").replace(" ", "")


def _ctl(panel):
    kids = [c for c in panel.children if "m-only" in c.classes]
    return kids[0] if kids else None


def test_role_and_delete_copies_open_inside_the_user_and_match_the_originals():
    root = _tree(_users())
    _, mine = _user_blocks(root)[0]
    head, panel = _user_blocks(root)[1]
    assert _ctl(mine) is None, "no controls for the signed-in admin's own user"

    # Originals: on the header row, hidden on phones.
    sel = head.find_all(lambda n: n.tag == "select")
    dele = head.find_all(lambda n: n.tag == "button")
    assert len(sel) == 1 and len(dele) == 1
    assert "m-hide" in sel[0].classes and "m-hide" in dele[0].classes

    # Copies: the panel's first element, phone-only.
    ctl = _ctl(panel)
    assert ctl is panel.children[0]
    csel = ctl.find_all(lambda n: n.tag == "select")
    cdel = ctl.find_all(lambda n: n.tag == "button")
    assert len(csel) == 1 and len(cdel) == 1
    for attr in ("data-change", "data-user-id"):
        assert csel[0].attrs.get(attr) == sel[0].attrs.get(attr), attr
    assert csel[0].attrs["data-user-id"] == "2"
    opts = lambda s: [(o.attrs.get("value"), "selected" in o.attrs) for o in s.find_all(lambda n: n.tag == "option")]
    assert opts(csel[0]) == opts(sel[0]) == [("user", False), ("manager", True), ("admin", False)]
    for attr in ("hx-post", "hx-target", "hx-swap", "hx-confirm"):
        assert cdel[0].attrs.get(attr) == dele[0].attrs.get(attr), attr
    assert cdel[0].attrs["hx-post"] == "/admin/action/remove-user/2"
    assert cdel[0].attrs.get("type") == "button"


def test_a_manager_gets_no_copy_for_an_admin():
    root = _tree(_users(admin_role="manager"))
    blocks = _user_blocks(root)
    assert _ctl(blocks[1][1]) is not None          # a manager may change a manager
    assert _ctl(blocks[2][1]) is None              # but not an admin
    assert not blocks[2][0].find_all(lambda n: n.tag == "select")
    opts = [o.attrs["value"] for o in _ctl(blocks[1][1]).find_all(lambda n: n.tag == "option")]
    assert opts == ["user", "manager"]


def test_set_role_reads_the_user_from_the_element_itself():
    """Both copies are bound to the same handler; it must take the user from
    the element that changed, not from where it sits."""
    src = source("admin.html")
    fn = src[src.index("window.adminSetRole"):]
    fn = fn[:fn.index("};")]
    assert "this.dataset.userId" in fn and "this.value" in fn
    assert "querySelector" not in fn and "closest" not in fn


def test_char_rows_allowlist_rows_and_add_form_have_phone_hooks():
    root = _tree(_users())
    chars = root.find_all(lambda n: "adm-char" in n.classes)
    assert len(chars) == 3
    for row in chars:
        names = [k for c in row.children for k in c.classes if k.startswith("adm-char-")]
        assert names == ["adm-char-name", "adm-char-corp", "adm-char-tok", "adm-char-sync", "adm-char-btns"]
        btns = [c for c in row.children if "adm-char-btns" in c.classes][0]
        posts = [b.attrs.get("hx-post", "") for b in btns.find_all(lambda n: n.tag == "button")]
        assert posts[0].startswith("/admin/action/force-sync/")
        assert posts[1].startswith("/admin/action/remove-character/")
    allow = root.find_all(lambda n: "adm-allow" in n.classes)
    assert len(allow) == 2
    form = _by_id(root, "allow-search").parent.parent
    assert "m-stack" in form.classes
    results = _by_id(root, "allow-results")
    assert "display:none" in results.attrs["style"].replace(" ", "")


def test_user_row_hides_the_character_count_on_phones():
    root = _tree(_users())
    head, _ = _user_blocks(root)[1]
    count = head.find_all(lambda n: n.all_text() == "2 chars")
    assert count and "m-hide" in count[-1].classes
    name = head.find_all(lambda n: "adm-user-name" in n.classes)
    assert name and name[0].all_text() == "Sample Pilot Bravo"


# ── Audit log (D17 A) ─────────────────────────────────────────────────

LONG_DETAIL = ("Role changed from user to manager by Sample Pilot Alpha after a review "
               "of the allowlist entries and the characters attached to this account.")


def _audit():
    ev = lambda i, kind, cid, detail, mins: _NS(
        id=i, event_type=kind, character_id=cid, detail=detail,
        created_at=NOW - timedelta(minutes=mins))
    events = [ev(1, "admin_set_role", 90000002, LONG_DETAIL, 4),
              ev(2, "admin_update_report_acknowledged", 90000001, "v1.9.0 report", 75),
              ev(3, "sync_error", None, None, 200)]
    return render_page(admin_mod, "partials/admin_audit.html", "/admin/section/audit",
                       events=events, char_names={90000001: "Sample Pilot Alpha",
                                                  90000002: "Sample Pilot Bravo"},
                       filter="", audit_filters=admin_mod.AUDIT_FILTERS, format_age=_age)


def test_audit_rows_follow_the_contract():
    assert len(LONG_DETAIL) > 100
    html = _audit()
    rows = assert_mrow(html, 3)
    assert len(rows) == 3
    for r in cells_rows(html):
        assert r["attrs"].get("data-click") == "toggleMRow"
        assert "b-table-row" in r["attrs"]["class"].split()
        assert_single_value_child(r)


def test_audit_row_is_event_character_age_with_the_full_detail_inside():
    first, second, third = cells_rows(_audit())
    lead = row_lead(first)
    assert len(lead) == 1 and lead[0]["text"] == "admin_set_role"
    assert [k["text"] for k in row_keys(first)] == ["Sample Pilot Bravo", "4m ago"]
    labelled = row_labelled(first)
    assert list(labelled) == ["Name", "Event", "Detail", "When"]
    assert labelled["Name"]["text"] == "Sample Pilot Bravo"
    assert labelled["Event"]["text"] == "admin_set_role"
    assert labelled["Detail"]["text"] == LONG_DETAIL
    assert labelled["When"]["text"] == "2030-01-01 11:56:00 UTC"
    for c in labelled.values():
        assert "m-only" in c["attrs"].get("class", "").split()
    # Key 1 is the phone-only character copy; the desktop cells are untouched.
    assert "m-only" in row_keys(first)[0]["attrs"]["class"].split()
    desktop_detail = [c for c in first["cells"] if c["attrs"].get("title") == LONG_DETAIL]
    assert len(desktop_detail) == 1
    assert desktop_detail[0]["text"] == LONG_DETAIL[:100].strip()
    assert "data-m" not in desktop_detail[0]["attrs"] and "data-m-label" not in desktop_detail[0]["attrs"]

    assert [k["text"] for k in row_keys(second)] == ["Sample Pilot Alpha", "1h ago"]
    # No character, no detail: a dash keeps key 1 readable; no empty cells.
    assert [k["text"] for k in row_keys(third)] == ["—", "3h ago"]
    assert list(row_labelled(third)) == ["Event", "When"]


def test_audit_lead_badge_carries_no_inline_hide_and_head_row_is_hidden():
    root = _tree(_audit())
    heads = root.find_all(lambda n: "m-head" in n.classes)
    assert len(heads) == 1 and "b-table-row" in heads[0].classes
    for row in root.find_all(lambda n: "m-row" in n.classes):
        lead = [c for c in row.children if c.attrs.get("data-m") == "lead"][0]
        assert "display" not in lead.attrs.get("style", "")
        assert lead.find_all(lambda n: "b-badge" in n.classes)


def test_audit_filter_select_keeps_its_handler_and_gets_the_full_width_hook():
    root = _tree(_audit())
    sel = root.find_all(lambda n: n.tag == "select")
    assert len(sel) == 1
    assert sel[0].attrs.get("data-change") == "adminAuditFilter"
    assert "adm-audit-filter" in sel[0].classes


# ── CSS (R6 T6 section) ───────────────────────────────────────────────

def _phone():
    return phone_block(_section("T6"))[0]


def test_section_is_phone_only():
    phone, after = phone_block(_section("T6"))
    assert phone.strip()
    assert not after.strip(), "admin needs no desktop rule"


def test_two_line_rows_override_inline_widths_and_wrap():
    css = _phone()
    row = rule_bodies(css, ".adm-2l")
    assert "flex-wrap: wrap !important" in row
    kids = rule_bodies(css, ".adm-2l > *")
    for decl in ("flex: none !important", "width: auto !important", "min-width: 0 !important",
                 "font-size: 11px !important"):
        assert decl in kids, decl
    main = rule_bodies(css, ".adm-2l > .adm-2l-main")
    assert "flex: 1 1 0 !important" in main and "text-overflow: ellipsis" in main
    assert "order: 3" in rule_bodies(css, ".adm-2l::after")
    assert "flex: 0 0 100%" in rule_bodies(css, ".adm-2l::after")
    assert "margin-left: auto" in rule_bodies(css, ".adm-2l > .adm-2l-time")
    # Cells with no line role (none today) stay out of the way.
    assert "display: none !important" in rule_bodies(
        css, ".adm-2l > :not(.adm-2l-a, .adm-2l-main, .adm-2l-time, .adm-2l-sub)")


def test_char_and_history_rows_wrap_to_two_lines():
    css = _phone()
    assert "flex-wrap: wrap !important" in rule_bodies(css, ".adm-char")
    assert "flex: 0 0 100%" in rule_bodies(css, ".adm-char::after")
    assert "flex: 1 1 0 !important" in rule_bodies(css, ".adm-char > .adm-char-name")
    assert "flex: 1 1 0 !important" in rule_bodies(css, ".adm-char > .adm-char-corp")
    hist = rule_bodies(css, "#updater-panel .upd-table tr")
    assert "flex-wrap: wrap" in hist
    assert "display: none" in rule_bodies(css, "#updater-panel .upd-table thead")


def test_small_buttons_get_width_never_a_smaller_height():
    """Buttons, selects and inputs already get 44px height on phones; the
    section only widens the narrow ones and never sets a 40px height."""
    css = _phone()
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        if not re.search(r"(?<![-\w])(min-)?height:\s*40px", m.group(2)):
            continue
        for sel in selectors(m.group(1)):
            last = re.split(r"[\s>+~]+", sel.strip())[-1]
            assert not re.match(r"(button|select|input|textarea)\b|.*\.b-btn\b", last), sel
    for sel in (".adm-char-btns > .b-btn", ".adm-allow > .b-btn", ".adm-2l-act > button"):
        assert "min-width: 44px" in rule_bodies(css, sel), sel
    for name in ("admin.html", "partials/admin_users.html", "partials/admin_esi.html",
                 "partials/admin_scheduler.html", "partials/admin_audit.html"):
        assert "m-tap" not in source(name), name


def test_updater_summaries_grow_without_losing_their_marker():
    css = _phone()
    for sel in ("#updater-panel .updater-outcome details > summary",
                "#updater-panel .updater-schedule-form > summary"):
        body = rule_bodies(css, sel)
        assert "min-height: 44px" in body, sel
        assert "display" not in body, sel
    for sel in ("#updater-panel .updater-policy > summary", "#updater-panel .updater-notify > summary"):
        assert "min-height: 44px" in rule_bodies(css, sel), sel
    assert "min-height: 40px" in rule_bodies(css, "#updater-panel .upd-status .b-row-val > a")


def test_no_display_rule_can_reach_elements_toggled_inline_or_by_hidden():
    """#updater-restarting is shown by removing `hidden`; #allow-results and
    the user panels by inline display. A display rule on any of them (or on
    .upd-note / bare p) would break that."""
    css = _phone()
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        if "display" not in m.group(2):
            continue
        for sel in selectors(m.group(1)):
            assert "upd-note" not in sel and "updater-restarting" not in sel, sel
            assert "allow-results" not in sel, sel
            assert not re.search(r"(^|\s|>)p(\s|$|:|\.)", sel), sel
            assert not re.search(r"\.adm-user\s*\+", sel), sel


def test_dropdown_buttons_are_styled_like_the_tab_links():
    css = _phone()
    body = rule_bodies(css, "#admin-tabs-m .m-tabs-list button")
    for decl in ("display: block", "width: 100%", "text-align: left", "text-transform: uppercase"):
        assert decl in body, decl
    assert "min-height" not in body, "buttons already get 44px on phones"
    assert "var(--accent)" in rule_bodies(css, "#admin-tabs-m .m-tabs-list button.is-active")


def test_audit_lead_badge_is_capped_so_key_one_keeps_its_room():
    css = _phone()
    body = rule_bodies(css, ".adm-audit-row > [data-m=\"lead\"] .b-badge")
    assert "max-width" in body and "text-overflow: ellipsis" in body
    assert "flex: 1 1 100%" in rule_bodies(css, ".adm-audit-filter")
