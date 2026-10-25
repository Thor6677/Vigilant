"""Regression tests for six small fixes batched together.

ISS-086  sync handler must re-raise SystemExit/KeyboardInterrupt, not swallow them
ISS-108  the dead journal-entries partial route is gone
ISS-109  notifications.js must not touch localStorage unguarded
ISS-115  the corp wallet chart's Total line must not use a CSS variable as a colour
ISS-116  renaming a dashboard group must carry its force-expanded state over
ISS-127  D-Scan/Local copy text must not carry HTML entities
"""
import ast
import json
import os
import re

import pytest
from fastapi.templating import Jinja2Templates

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


# ── ISS-086 ───────────────────────────────────────────────────────────────

def test_sync_base_exception_handler_reraises_and_leaves_no_dead_handler():
    tree = ast.parse(_read("app/routes/dashboard.py"))
    found = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        names = []
        for h in node.handlers:
            t = h.type
            if isinstance(t, ast.Tuple):
                names.append({getattr(e, "attr", getattr(e, "id", "")) for e in t.elts})
            else:
                names.append({getattr(t, "attr", getattr(t, "id", ""))} if t else {""})
        base = [i for i, n in enumerate(names) if "BaseException" in n]
        if not base:
            continue
        found = True
        # An `except Exception` after `except BaseException` can never run.
        assert not any("Exception" in n and "BaseException" not in n for n in names[base[0] + 1:])
        body_src = ast.unparse(node.handlers[base[0]])
        assert "not isinstance(e, Exception)" in body_src and "raise" in body_src
    assert found, "the sync BaseException handler moved; update this test"


# ── ISS-108 ───────────────────────────────────────────────────────────────

def test_dead_journal_entries_route_is_removed():
    from app.routes.journal import router

    assert not [r for r in router.routes if r.path.endswith("/journal-entries")]
    assert not os.path.exists(os.path.join(ROOT, "app/templates/partials/journal_entries.html"))


# ── ISS-109 ───────────────────────────────────────────────────────────────

def test_notifications_js_guards_every_localstorage_access():
    code = re.sub(r"/\*.*?\*/", "", _read("static/js/notifications.js"), flags=re.S)
    offenders = [
        line.strip() for line in code.splitlines()
        if "localStorage." in line and not re.match(r"\s*function ls(Get|Set|Remove)\(", line)
    ]
    assert offenders == []


# ── ISS-115 ───────────────────────────────────────────────────────────────

def test_corp_wallet_total_line_resolves_the_accent_colour():
    src = _read("app/templates/corporations.html")
    total = re.search(r"label: 'Total'.*?borderColor: ([^,]+),", src, flags=re.S)
    assert total, "Total dataset moved; update this test"
    assert "var(" not in total.group(1)
    assert "getPropertyValue('--accent')" in src


# ── ISS-116 ───────────────────────────────────────────────────────────────

def test_group_rename_updates_force_expanded_groups():
    src = _read("app/templates/dashboard.html")
    commit = src[src.index("function commit()"):]
    commit = commit[:commit.index("scheduleSave();")]
    assert "forceExpandedGroups.indexOf(oldName)" in commit
    assert "forceExpandedGroups[fx] = newName" in commit


# ── ISS-118 ───────────────────────────────────────────────────────────────

def test_group_rename_finds_the_body_in_compact_groups_too():
    """The rename path looks the group body up with the same selector as the
    expand and collapse code, not `.sortable-group` alone."""
    src = _read("app/templates/dashboard.html")
    refresh = src[src.index("// The toggle's title names the group: refresh it."):]
    refresh = refresh[:refresh.index("dashSetGroupToggle(")]
    assert "var groupBody = section.querySelector('.sortable-group, .dash-compact-group-body');" in refresh


# ── ISS-127 ───────────────────────────────────────────────────────────────

_PUSH = re.compile(r"lines\.push\(\{\{.*?\}\}\);")


@pytest.mark.parametrize("rel", ["app/templates/intel_local.html", "app/templates/intel_dscan.html"])
def test_copy_text_has_no_entities(rel):
    env = Jinja2Templates(directory="app/templates").env
    src = _read(rel)
    # No copy line may still interpolate a name inside a quoted JS string.
    # The two integer totals are the only values that are safe that way.
    quoted = re.findall(r"lines\.push\('[^']*\{\{\s*([^}]*?)\s*\}\}", src)
    assert set(quoted) <= {"summary.resolved", "summary.ships"}
    pushes = _PUSH.findall(src)
    assert pushes, "copy builders moved; update this test"
    ctx = {
        "count": 3, "alliance_name": "Sample's <Alliance> & Co", "corp_name": "Sample's Corp",
        "cls": "Cap's <Ship>", "group": {"group": "Frig's", "total": 5},
        "ship": {"count": 2, "name": "Rifter's", "class": "Frig"},
    }
    for p in pushes:
        out = env.from_string(p).render(**ctx)
        arg = out[len("lines.push("):-2]
        text = json.loads(arg)
        assert "&#" not in text and "&amp;" not in text and "&lt;" not in text
        assert "'" in text or "<" in text or "&" in text
