"""Routes for /industry/copy-packs (app/routes/copy_packs.py).

get_db-override idiom from tests/test_build_finder.py: a real temp SQLite DB
seeded with a two-level blueprint tree and two users' characters. ESI is
never reached: the blueprint and location gatherers are monkeypatched.
CSRF idiom from tests/test_compression_hub_pricing.py."""
import asyncio
import base64
import json
import os
import tempfile
from datetime import datetime

import itsdangerous
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.main as main
import app.routes.copy_packs as copy_packs
from app.db.models import Base, Character, User, get_db
from app.db.sde_models import SDEBlueprintInfo, SDEBlueprintMaterial, SDEType
from app.industry import copy_pack as cp
from tests.conftest import ensure_user

USER_ID, OTHER_USER_ID = 41, 42
CHAR_ID, OTHER_CHAR_ID = 95000001, 95000002
HULL, HULL_BP = 23757, 23758
PLATES, PLATES_BP = 21017, 21018
_CSRF = "test-csrf-token-copy-packs-00001"


def _run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def _client(user_id=USER_ID):
    ensure_user(user_id)
    signer = itsdangerous.TimestampSigner(main.settings.secret_key)
    payload = {"user_id": user_id, "csrf_token": _CSRF}
    cookie = signer.sign(base64.b64encode(json.dumps(payload).encode())).decode()
    client = TestClient(main.app, base_url="https://testserver")
    client.cookies.set("vigilant_session", cookie)
    return client


def _char(cid, user_id, name):
    return Character(character_id=cid, character_name=name, user_id=user_id,
                     access_token="a", refresh_token="r",
                     token_expiry=datetime(2099, 1, 1), scopes="",
                     corporation_id=98000001, corporation_name="Test Corp")


def _seeded_db():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    async def seed():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as db:
            db.add(User(id=USER_ID))
            db.add(User(id=OTHER_USER_ID))
            db.add(_char(CHAR_ID, USER_ID, "Builder"))
            db.add(_char(OTHER_CHAR_ID, OTHER_USER_ID, "Stranger"))
            db.add(SDEType(type_id=HULL, type_name="Archon", published=True))
            db.add(SDEType(type_id=PLATES, type_name="Capital Armor Plates", published=True))
            db.add(SDEType(type_id=34, type_name="Tritanium", published=True))
            db.add(SDEBlueprintInfo(blueprint_type_id=HULL_BP, product_type_id=HULL,
                                    product_quantity=1, manufacturing_time=1800000,
                                    max_production_limit=1, copying_time=1600000))
            db.add(SDEBlueprintInfo(blueprint_type_id=PLATES_BP, product_type_id=PLATES,
                                    product_quantity=1, manufacturing_time=16000,
                                    max_production_limit=40, copying_time=12800))
            db.add(SDEBlueprintMaterial(blueprint_type_id=HULL_BP, activity_id=1,
                                        material_type_id=PLATES, quantity=6))
            db.add(SDEBlueprintMaterial(blueprint_type_id=PLATES_BP, activity_id=1,
                                        material_type_id=34, quantity=1000))
            await db.commit()

    _run(seed())

    async def override_get_db():
        async with SessionLocal() as session:
            yield session

    main.app.dependency_overrides[get_db] = override_get_db

    def teardown():
        main.app.dependency_overrides.pop(get_db, None)
        _run(engine.dispose())
        os.unlink(tmp.name)

    return teardown


def _stub_esi(monkeypatch, bpos=(), bpcs=(), notes=()):
    async def fake_owned(db, chars, selected):
        return list(bpos), list(bpcs), list(notes)

    async def fake_locations(db, plan, chars):
        return {}

    monkeypatch.setattr(copy_packs, "_owned_blueprints", fake_owned)
    monkeypatch.setattr(copy_packs, "_location_names", fake_locations)


def _plan(client, **fields):
    data = {"type_id": str(HULL), "quantity": "2", "character_id": str(CHAR_ID),
            "structure": "azbel", "rig": "none", "security": "nullsec",
            "state_json": "{}", "skills_json": "{}"}
    data.update(fields)
    return client.post("/industry/copy-packs/plan", data=data,
                       headers={"X-CSRF-Token": _CSRF})


HULL_BPO = cp.OwnedBlueprint(7001, HULL_BP, True, 10, 20, None,
                             owner_name="Builder", location_label="Personal Hangar")


# ── gating ───────────────────────────────────────────────────────────────────

def test_page_redirects_when_logged_out():
    r = TestClient(main.app).get("/industry/copy-packs", follow_redirects=False)
    assert r.status_code in (302, 307)


def test_search_401_when_logged_out():
    r = TestClient(main.app).get("/industry/copy-packs/search?q=arch")
    assert r.status_code == 401


def test_plan_refused_when_logged_out():
    r = TestClient(main.app, base_url="https://testserver").post(
        "/industry/copy-packs/plan", data={"type_id": str(HULL)})
    assert r.status_code in (401, 403)   # CSRF middleware may answer first


def test_plan_for_another_users_character_is_404(monkeypatch):
    teardown = _seeded_db()
    try:
        _stub_esi(monkeypatch)
        r = _plan(_client(), character_id=str(OTHER_CHAR_ID))
        assert r.status_code == 404
    finally:
        teardown()


# ── rendering ────────────────────────────────────────────────────────────────

def test_page_renders_for_a_logged_in_user():
    teardown = _seeded_db()
    try:
        r = _client().get("/industry/copy-packs")
        assert r.status_code == 200
        assert 'id="cp-form"' in r.text and "Builder" in r.text
        assert "Stranger" not in r.text
    finally:
        teardown()


def test_search_lists_manufacturable_products():
    teardown = _seeded_db()
    try:
        r = _client().get("/industry/copy-packs/search?q=arch")
        assert r.status_code == 200
        assert "Archon" in r.text and 'data-click="cpSelectProduct"' in r.text
    finally:
        teardown()


def test_plan_renders_groups_tree_and_skills(monkeypatch):
    teardown = _seeded_db()
    try:
        _stub_esi(monkeypatch, bpos=[HULL_BPO])
        r = _plan(_client(), state_json=json.dumps({"build": {str(PLATES): True}}))
        assert r.status_code == 200
        text = r.text
        assert "Copy (1)" in text                      # the Archon hull
        assert "2 × 1" in text                         # 2 copies of 1 run
        assert "Source elsewhere: no BPO (1)" in text  # plates: no BPO owned
        assert "Can&#39;t copy: buy or source (0)" in text or "Can't copy: buy or source (0)" in text
        assert 'id="cp-tree" hx-swap-oob="true"' in text
        assert 'id="cp-skills" hx-swap-oob="true"' in text
        assert "manual" in text                        # no synced skills
        assert "type_id=23758&amp;me=10&amp;te=20&amp;runs=1" in text
    finally:
        teardown()


def test_plan_quantity_is_clamped(monkeypatch):
    teardown = _seeded_db()
    try:
        _stub_esi(monkeypatch, bpos=[HULL_BPO])
        r = _plan(_client(), quantity="5000")
        assert "1000 × 1" in r.text
    finally:
        teardown()


def test_plan_without_a_product_prompts():
    teardown = _seeded_db()
    try:
        r = _plan(_client(), type_id="")
        assert r.status_code == 200 and "Pick a product" in r.text
    finally:
        teardown()
