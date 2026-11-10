"""Paste parsers run in linear time and paste inputs are size-capped (ISS-166).

Several paste parsers matched each line with a pattern shaped like
`^(.+?)\\s+<token>$`: a lazy group, a greedy run of spaces, then a token that
must follow. Python's regex engine retries every split point inside a long
run of spaces, so a line of "a", n spaces and "b" cost about n*n/2 steps,
and the parsers run on the single worker's event loop, so every other
request waited too. The hauling and appraisal POSTs also answered without a
session, although their pages are login-only.

The timing tests use lines long enough that the old patterns take seconds
while linear ones take well under a millisecond, so the budget sits far from
both. The route tests check the caps, which hold whatever the parser does.
"""
import asyncio
import base64
import json
import tempfile
import time

import itsdangerous
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, SkillPlan, User, get_db
from app.industry.hauling import parse_eve_paste
from app.routes import fitting as fitting_mod
from app.routes import industry as industry_mod
from app.routes import skill_plans as skill_plans_mod
from app.routes.dscan import _parse_distance

USER_ID = 9411
PLAN_ID = 941
_CSRF = "test-csrf-token-paste-caps-0123456789"

# Long enough that a quadratic pattern needs seconds, short enough that the
# old code still finishes when the test is run against it.
_RUN = 25_000
_BUDGET_S = 0.5


def _timed(fn, *args):
    start = time.perf_counter()
    out = fn(*args)
    return out, time.perf_counter() - start


def _assert_fast(fn, *args):
    out, elapsed = _timed(fn, *args)
    assert elapsed < _BUDGET_S, (
        f"{fn.__name__} took {elapsed:.2f}s on {len(args[0]):,} characters: "
        "the pattern backtracks over the run of spaces")
    return out


# ── The parsers themselves ──────────────────────────────────────────────────

@pytest.mark.parametrize("line", [
    "a" + " " * _RUN + "b",          # neither quantity pattern matches
    "a" + " " * _RUN + "x b",        # the "xN" pattern gets as far as the x
    "a" + " " * _RUN + "1 b",        # digits, then something after them
], ids=["spaces", "spaces-x", "spaces-digits"])
def test_hauling_paste_parser_is_linear_on_a_long_run_of_spaces(line):
    items = _assert_fast(parse_eve_paste, line)
    assert items == [{"name": line, "qty": 1}]


def test_hauling_paste_parser_still_reads_every_format():
    items = parse_eve_paste("\n".join([
        "Tritanium\t1,000\tMineral",
        "Pyerite x2,500",
        "Mexallon X 30",
        "Isogen    40",
        "Nocxium x x5",
        "Zydrine 0123",
        "Megacyte",
        "Tritanium 5",
    ]))
    assert items == [
        {"name": "Tritanium", "qty": 1005},
        {"name": "Pyerite", "qty": 2500},
        {"name": "Mexallon", "qty": 30},
        {"name": "Isogen", "qty": 40},
        {"name": "Nocxium x", "qty": 5},
        {"name": "Zydrine 0123", "qty": 1},
        {"name": "Megacyte", "qty": 1},
    ]


def test_dscan_distance_parser_is_linear_on_a_long_run_of_spaces():
    assert _assert_fast(_parse_distance, "1" + " " * _RUN + "x") is None


def test_dscan_distance_parser_still_reads_distances():
    assert _parse_distance("1,234 km") == 1_234_000
    assert _parse_distance("12 345 m") == 12_345
    assert _parse_distance("2.5 AU") == 2.5 * 149_597_870_700
    assert _parse_distance("-") is None


@pytest.mark.parametrize("line", [
    "a" + " " * _RUN + "b",          # the "xN" suffix pattern
    "a" + " [" * _RUN,               # the "[marker]" suffix pattern, never closed
], ids=["spaces", "open-brackets"])
def test_skill_plan_fitting_parser_is_linear_on_long_lines(line):
    ship, modules = _assert_fast(skill_plans_mod._parse_eft_text, "[Rifter, Fit]\n" + line)
    assert ship == "Rifter"
    assert modules == [line]


def test_skill_plan_fitting_parser_still_strips_suffixes():
    ship, modules = skill_plans_mod._parse_eft_text(
        "[Rifter, Fit]\nHobgoblin II x5\nDamage Control II [offline]\n"
        "Gyrostabilizer II  [a] [b]\n[Empty High slot]")
    assert ship == "Rifter"
    assert modules == ["Hobgoblin II", "Damage Control II", "Gyrostabilizer II"]


@pytest.mark.parametrize("line", [
    "a" + " " * _RUN + "b",
    "a" + " " * _RUN + "x",
], ids=["spaces", "spaces-x"])
def test_eft_quantity_suffix_is_linear(line):
    assert _assert_fast(fitting_mod._split_eft_quantity, line) == (line, None)


def test_eft_quantity_suffix_still_splits():
    assert fitting_mod._split_eft_quantity("Hobgoblin II x5") == ("Hobgoblin II", 5)
    assert fitting_mod._split_eft_quantity("Hobgoblin II   x12") == ("Hobgoblin II", 12)
    assert fitting_mod._split_eft_quantity("Hobgoblin II") == ("Hobgoblin II", None)


@pytest.mark.parametrize("header", [
    "[" + "," * _RUN,                 # never closed: every comma was a candidate
    "[a," + " " * _RUN + "b",         # the spaces after the comma
], ids=["commas", "spaces"])
def test_eft_header_is_linear(header):
    assert _assert_fast(fitting_mod._split_eft_header, header) is None


def test_eft_header_still_splits():
    assert fitting_mod._split_eft_header("[Rifter, My Fit]") == ("Rifter", "My Fit")
    assert fitting_mod._split_eft_header("[Rifter,Fit, with comma]") == ("Rifter", "Fit, with comma")
    assert fitting_mod._split_eft_header("[Rifter]") is None


@pytest.mark.parametrize("line", [
    "a" + " " * _RUN + "b",
    "a" + " " * _RUN + "IV b",
], ids=["spaces", "spaces-level"])
def test_skill_level_suffix_is_linear(line):
    assert _assert_fast(skill_plans_mod._split_skill_level, line) is None


def test_skill_level_suffix_still_splits():
    assert skill_plans_mod._split_skill_level("Gunnery V") == ("Gunnery", "V")
    assert skill_plans_mod._split_skill_level("Small Hybrid Turret   IV") == ("Small Hybrid Turret", "IV")
    assert skill_plans_mod._split_skill_level("Drones 3") == ("Drones", "3")
    assert skill_plans_mod._split_skill_level("Skill I I") == ("Skill I", "I")
    assert skill_plans_mod._split_skill_level("Gunnery") is None


# ── The routes ──────────────────────────────────────────────────────────────

def _run_async(fn):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(fn())
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def _cookie(**data):
    import app.main as main
    signer = itsdangerous.TimestampSigner(main.settings.secret_key)
    return signer.sign(base64.b64encode(json.dumps(data).encode())).decode()


@pytest.fixture
def app_db():
    """A temp DB holding one user and one personal skill plan they own."""
    import app.main as main

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    async def _setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as db:
            db.add(User(id=USER_ID))
            db.add(SkillPlan(id=PLAN_ID, user_id=USER_ID, name="Caps"))
            await db.commit()

    _run_async(_setup)

    async def override_get_db():
        async with SessionLocal() as session:
            yield session

    main.app.dependency_overrides[get_db] = override_get_db
    try:
        yield main.app
    finally:
        main.app.dependency_overrides.pop(get_db, None)

        async def _dispose():
            await engine.dispose()
        _run_async(_dispose)


def _client(app, **session):
    client = TestClient(app, base_url="https://testserver", follow_redirects=False)
    client.cookies.set("vigilant_session", _cookie(csrf_token=_CSRF, **session))
    return client


@pytest.fixture
def lookups(monkeypatch):
    """Counts SDE name lookups: a refused paste must not reach any."""
    calls = []

    async def _lookup(db, name):
        calls.append(name)
        return None

    monkeypatch.setattr(industry_mod.sde, "type_name_to_id", _lookup)
    return calls


_PASTE_ROUTES = ("/industry/hauling/resolve", "/industry/appraisal/calculate")


@pytest.mark.parametrize("path", _PASTE_ROUTES)
def test_paste_routes_refuse_a_visitor_without_a_session(app_db, lookups, path):
    r = _client(app_db).post(path, data={"paste_text": "Tritanium 10"},
                             headers={"X-CSRF-Token": _CSRF})
    assert r.status_code == 401 and r.text == ""
    assert lookups == []


@pytest.mark.parametrize("path", _PASTE_ROUTES)
def test_paste_routes_refuse_too_many_lines(app_db, lookups, path):
    paste = "\n".join(f"Item {i}" for i in range(industry_mod.MAX_PASTE_LINES + 1))
    r = _client(app_db, user_id=USER_ID).post(path, data={"paste_text": paste},
                                              headers={"X-CSRF-Token": _CSRF})
    assert r.status_code == 200
    assert f"the limit is {industry_mod.MAX_PASTE_LINES:,}" in r.text
    assert lookups == []


@pytest.mark.parametrize("path", _PASTE_ROUTES)
def test_paste_routes_refuse_an_overlong_line(app_db, lookups, path):
    paste = "Tritanium 5\n" + "a" + " " * _RUN + "b"
    r = _client(app_db, user_id=USER_ID).post(path, data={"paste_text": paste},
                                              headers={"X-CSRF-Token": _CSRF})
    assert r.status_code == 200
    assert "Line 2 is" in r.text
    assert f"the limit is {industry_mod.MAX_PASTE_LINE_CHARS:,}" in r.text
    assert lookups == []


@pytest.mark.parametrize("path", _PASTE_ROUTES)
def test_paste_routes_still_parse_a_normal_paste(app_db, lookups, path):
    r = _client(app_db, user_id=USER_ID).post(path, data={"paste_text": "Tritanium 5\nPyerite x2"},
                                              headers={"X-CSRF-Token": _CSRF})
    assert r.status_code == 200
    assert "No items could be resolved" in r.text
    assert lookups == ["Tritanium", "Pyerite"]


def test_eft_import_refuses_an_overlong_header(app_db):
    r = _client(app_db, user_id=USER_ID).post(
        "/tools/fitting/import-eft", json={"eft": "[" + "," * _RUN},
        headers={"X-CSRF-Token": _CSRF})
    assert r.status_code == 200
    assert "Line 1 is" in r.json()["error"]


def test_eft_import_refuses_too_many_lines(app_db):
    eft = "[Rifter, Fit]\n" + "\n".join(["Damage Control II"] * fitting_mod.EFT_MAX_LINES)
    r = _client(app_db, user_id=USER_ID).post(
        "/tools/fitting/import-eft", json={"eft": eft}, headers={"X-CSRF-Token": _CSRF})
    assert r.status_code == 200
    assert f"the limit is {fitting_mod.EFT_MAX_LINES:,}" in r.json()["error"]


def test_skill_import_refuses_too_many_lines(app_db):
    text = "\n".join(["Gunnery V"] * (skill_plans_mod.SKILL_TEXT_MAX_LINES + 1))
    r = _client(app_db, user_id=USER_ID).post(
        f"/skill-plans/{PLAN_ID}/import", data={"skill_text": text},
        headers={"X-CSRF-Token": _CSRF})
    assert r.status_code == 200
    assert f"the limit is {skill_plans_mod.SKILL_TEXT_MAX_LINES:,}" in r.text


def test_skill_import_refuses_an_overlong_line(app_db):
    r = _client(app_db, user_id=USER_ID).post(
        f"/skill-plans/{PLAN_ID}/import", data={"skill_text": "a" + " " * _RUN + "b"},
        headers={"X-CSRF-Token": _CSRF})
    assert r.status_code == 200
    assert "Line 1 is" in r.text


def test_skill_import_from_fitting_refuses_an_overlong_line(app_db):
    r = _client(app_db, user_id=USER_ID).post(
        f"/skill-plans/{PLAN_ID}/from-fitting",
        data={"fitting_text": "[Rifter, Fit]\na" + " [" * _RUN},
        headers={"X-CSRF-Token": _CSRF})
    assert r.status_code == 200
    assert "Line 2 is" in r.text
