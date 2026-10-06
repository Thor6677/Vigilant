"""Mobile R2 guard: the shared skill plan page carries no actions.js bindings
for an anonymous visitor.

`/skill-plans/shared/{token}` is public, and R2 reworks its template for
phones. base.html loads actions.js only for a session with user_id, so any
data-click (or other actions.js binding) on what a stranger sees is dead
for them; that includes an m-row's tap-to-open ("no tap-to-open on
anonymous pages" in the phone contract). tests/test_route_auth_gating.py's
sweep catches exactly this, but only for literal GET routes: it skips
parametrised paths like this one, because a made-up token would just
answer 404. So this test renders the template directly, through the route
module's own env, with an anonymous request and the context the route
builds for a plan with entries. Names and ids are invented."""
import re
import types

from app.routes import skill_plans as skill_plans_mod
from tests._mobile import render_page
from tests.test_route_auth_gating import _ACTION_BINDING

_NS = types.SimpleNamespace
_TOKEN = "sample-share-token"
# The script tag itself: base.html also names actions.js in comments inside
# scripts it renders for everyone.
_ACTIONS_SCRIPT = re.compile(r'<script\b[^>]*\bsrc="[^"]*/actions\.js\b')


def _entry(i, name, level, rank, primary, secondary):
    """One entry as shared_plan() builds it from the plan and the SDE."""
    return {"id": i, "skill_type_id": 3300 + i, "skill_name": name, "target_level": level,
            "rank": rank, "primary_attr_name": primary, "secondary_attr_name": secondary}


def _render_shared(**session):
    plan = _NS(id=7, name="Sample Doctrine Plan", share_token=_TOKEN,
               visibility="personal", description="")
    entries = [
        _entry(1, "Sample Gunnery Skill", 5, 1.0, "Perception", "Willpower"),
        _entry(2, "Sample Navigation Skill", 4, 3.0, "Intelligence", "Perception"),
        _entry(3, "Sample Drone Skill", 3, 1.5, "Memory", "Perception"),
    ]
    return render_page(skill_plans_mod, "skill_plan_shared.html", f"/skill-plans/shared/{_TOKEN}",
                       **session, plan=plan, entries=entries, characters=[], share_token=_TOKEN)


def test_anonymous_shared_plan_has_no_actions_js_bindings():
    html = _render_shared(session=None)
    assert "Sample Doctrine Plan" in html and "Sample Drone Skill" in html
    hit = _ACTION_BINDING.search(html)
    assert not hit, f"anonymous shared plan uses {hit.group(0).strip()}, but actions.js isn't loaded for it"
    assert not _ACTIONS_SCRIPT.search(html)


def test_logged_in_render_loads_actions_js():
    """Control: the same render with the default logged-in session loads
    actions.js and matches the binding pattern (base.html's own nav), so the
    anonymous test is passing because of the session, not by accident."""
    html = _render_shared()
    assert _ACTIONS_SCRIPT.search(html)
    assert _ACTION_BINDING.search(html)
