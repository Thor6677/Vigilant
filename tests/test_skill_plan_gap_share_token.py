"""ISS-163: the gap partial needs the share token, not just a shared plan.

GET /skill-plans/{plan_id}/gap/{character_id} used to fall back to any plan
with sharing turned on, so any signed-in user could read another user's plan
by walking plan ids. The fallback now needs the plan's current share token,
and a plan the caller cannot see answers exactly like one that does not exist.

ESI is stubbed at the route module's seams. Names, ids and tokens are invented.
"""
import pytest

from app.db.models import SkillPlan, SkillPlanEntry
from app.db.sde_models import SDEType
from tests._mobile import render_page
from tests.test_permissions_flow import (  # noqa: F401 — `env` is a fixture
    MAIN_ID, OTHER_ID, STRANGER_ID, USER_ID, env,
)

TOKEN = "Sample-Token-123"
SKILLS = {3301: "Sample Gunnery Skill", 3302: "Sample Navigation Skill"}
EMPTY = '<div class="b-empty">No skills in this plan.</div>'


@pytest.fixture
def gap(env, monkeypatch):
    """`env` plus a personal plan owned by OTHER_ID, shared by link."""
    import app.esi.character as esi_char
    from app.routes import skill_plans as sp

    async def seed(db):
        plan = SkillPlan(user_id=OTHER_ID, name="Sample Doctrine Plan",
                         visibility="personal", share_token=TOKEN)
        db.add(plan)
        await db.flush()
        for i, type_id in enumerate(SKILLS):
            db.add(SkillPlanEntry(plan_id=plan.id, skill_type_id=type_id,
                                  target_level=4, sort_order=i))
            db.add(SDEType(type_id=type_id, type_name=SKILLS[type_id]))
        await db.commit()
        return plan.id

    env.plan_id = env.q(seed)

    async def fake_refresh(char, db):
        return "stub-access-token"

    async def fake_skills(client, character_id):
        return {"total_sp": 5_000_000, "skills": []}

    async def fake_attributes(client, character_id):
        return {"charisma": 20, "intelligence": 20, "memory": 20,
                "perception": 20, "willpower": 20}

    async def fake_implants(client, character_id):
        return []

    async def fake_prices():
        return 900_000_000.0, 150_000_000.0

    monkeypatch.setattr(sp, "refresh_token", fake_refresh)
    monkeypatch.setattr(sp, "_get_injector_prices", fake_prices)
    monkeypatch.setattr(esi_char, "get_skills", fake_skills)
    monkeypatch.setattr(esi_char, "get_attributes", fake_attributes)
    # The route calls it inside its own try; the module has no such function
    # today, so it is added for the test rather than replaced.
    monkeypatch.setattr(esi_char, "get_implants", fake_implants, raising=False)

    def set_token(value):
        async def go(db):
            plan = await db.get(SkillPlan, env.plan_id)
            plan.share_token = value
            await db.commit()
        env.q(go)

    env.set_token = set_token
    env.other = lambda: env.client({"user_id": OTHER_ID})
    return env


def _get(client, plan_id, character_id, token=None):
    url = f"/skill-plans/{plan_id}/gap/{character_id}"
    return client.get(url, params={"token": token} if token is not None else None)


def _shows_plan(r):
    return all(name in r.text for name in SKILLS.values())


def test_the_right_token_shows_the_plan(gap):
    """The control for the refusals below: with the token, a viewer who has
    no other access gets the full analysis against their own character."""
    r = _get(gap.user(), gap.plan_id, MAIN_ID, token=TOKEN)
    assert r.status_code == 200
    assert _shows_plan(r), r.text


def test_a_shared_plan_is_not_readable_without_the_token(gap):
    r = _get(gap.user(), gap.plan_id, MAIN_ID)
    assert r.status_code == 200
    assert r.text == EMPTY
    assert not any(name in r.text for name in SKILLS.values())


def test_a_wrong_token_is_refused(gap):
    for wrong in ("Sample-Token-124", "", TOKEN[:-1], "é" * 16):
        r = _get(gap.user(), gap.plan_id, MAIN_ID, token=wrong)
        assert r.status_code == 200, wrong
        assert r.text == EMPTY, wrong


def test_a_rotated_token_stops_working(gap):
    """Unsharing and sharing again issues a new token. Whoever kept the old
    link loses access to the gap analysis as well as to the page."""
    gap.set_token("Sample-Token-NEW")
    assert _get(gap.user(), gap.plan_id, MAIN_ID, token=TOKEN).text == EMPTY
    assert _shows_plan(_get(gap.user(), gap.plan_id, MAIN_ID, token="Sample-Token-NEW"))


def test_an_unviewable_plan_answers_the_same_whether_shared_or_not(gap):
    """No existence oracle: with a character the caller does not own (so no
    ESI call is made), a plan they cannot see answers the same whether link
    sharing is on, off, or the plan does not exist at all."""
    user = gap.user()
    shared = _get(user, gap.plan_id, STRANGER_ID).text
    gap.set_token(None)
    unshared = _get(user, gap.plan_id, STRANGER_ID).text
    missing = _get(user, gap.plan_id + 1000, STRANGER_ID).text
    assert shared == unshared == missing == EMPTY


def test_the_owner_needs_no_token(gap):
    r = _get(gap.other(), gap.plan_id, STRANGER_ID)
    assert r.status_code == 200
    assert _shows_plan(r), r.text


def test_the_shared_page_sends_the_token_with_the_gap_request():
    """The shared page is how a link holder reaches the gap partial, so its
    script must pass the token along; plan.id alone no longer works."""
    import types
    from app.routes import skill_plans as sp

    plan = types.SimpleNamespace(id=7, name="Sample Doctrine Plan", share_token=TOKEN,
                                 visibility="personal", description="")
    char = types.SimpleNamespace(character_id=MAIN_ID, character_name="Main Pilot")
    html = render_page(sp, "skill_plan_shared.html", f"/skill-plans/shared/{TOKEN}",
                       plan=plan, entries=[], characters=[char], share_token=TOKEN)
    script = html[html.index("function runGap()"):]
    script = script[:script.index("</script>")]
    assert "/skill-plans/7/gap/" in script
    assert f'"{TOKEN}"' in script and "encodeURIComponent(" in script
    assert "?token=" in script
