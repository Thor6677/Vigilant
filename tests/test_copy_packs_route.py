"""Routes for /industry/copy-packs (app/routes/copy_packs.py).

get_db-override idiom from tests/test_build_finder.py: a real temp SQLite DB
seeded with a two-level blueprint tree and two users' characters. ESI is
never reached: the blueprint and location gatherers are monkeypatched.
CSRF idiom from tests/test_compression_hub_pricing.py."""
import asyncio
import base64
import json
import os
import re
import tempfile
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime

import itsdangerous
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.main as main
import app.routes.copy_packs as copy_packs
from app.auth import scopes as perms
from app.db.cache import TTL, _ttl_for_path
from app.db.models import Base, Character, CharacterDashboardCache, User, get_db
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

    teardown.session = SessionLocal
    return teardown


def _stub_esi(monkeypatch, bpos=(), bpcs=(), notes=()):
    async def fake_owned(db, chars, selected):
        return list(bpos), list(bpcs), list(notes), {}

    async def fake_locations(db, plan, candidates):
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
        assert "Make copies (1)" in text                # the Archon hull
        assert "2 × 1" in text                         # 2 copies of 1 run
        assert "Source elsewhere: no BPO (1)" in text  # plates: no BPO owned
        assert "Can&#39;t copy: buy or source (0)" in text or "Can't copy: buy or source (0)" in text
        assert 'id="cp-tree" class="cp-drill" hx-swap-oob="true"' in text
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


def test_early_exit_clears_the_left_column(monkeypatch):
    teardown = _seeded_db()
    try:
        _stub_esi(monkeypatch)
        r = _plan(_client(), character_id="")
        assert "Pick a character." in r.text
        assert '<div id="cp-skills" hx-swap-oob="true"></div>' in r.text
        assert '<div id="cp-tree" hx-swap-oob="true"></div>' in r.text
    finally:
        teardown()


def test_mfg_link_carries_the_facility(monkeypatch):
    teardown = _seeded_db()
    try:
        _stub_esi(monkeypatch, bpos=[HULL_BPO])
        r = _plan(_client(), structure="azbel", rig="none", security="nullsec")
        assert "&amp;structure=azbel&amp;rig=none&amp;security=nullsec" in r.text
    finally:
        teardown()


def test_form_opts_out_of_the_global_error_pill():
    teardown = _seeded_db()
    try:
        r = _client().get("/industry/copy-packs")
        assert 'id="cp-form"' in r.text and 'data-htmx-no-error="1"' in r.text
        assert "htmx:responseError" in r.text and "htmx:sendError" in r.text
    finally:
        teardown()


def test_search_escapes_type_names():
    teardown = _seeded_db()
    try:
        async def add():
            async with teardown.session() as db:
                db.add(SDEType(type_id=99001, type_name='Zq"<b>x', published=True))
                db.add(SDEBlueprintInfo(blueprint_type_id=99002, product_type_id=99001,
                                        product_quantity=1, manufacturing_time=10,
                                        max_production_limit=1, copying_time=10))
                db.add(SDEBlueprintMaterial(blueprint_type_id=99002, activity_id=1,
                                            material_type_id=34, quantity=1))
                await db.commit()
        _run(add())
        r = _client().get("/industry/copy-packs/search?q=Zq")
        assert 'data-type-name="Zq&#34;&lt;b&gt;x"' in r.text
        assert "<b>x" not in r.text
    finally:
        teardown()


# ── _skills ──────────────────────────────────────────────────────────────────

def test_synced_skills_are_labelled_from_esi_and_overrides_manual(monkeypatch):
    teardown = _seeded_db()
    try:
        async def sync():
            async with teardown.session() as db:
                char = (await db.execute(select(Character).where(
                    Character.character_id == CHAR_ID))).scalar_one()
                char.scopes = perms.SKILLS
                db.add(CharacterDashboardCache(character_id=CHAR_ID, skills_json=json.dumps(
                    {"total_sp": 0, "unallocated_sp": 0,
                     "levels": {str(cp.MASS_PRODUCTION): 4}})))
                await db.commit()
        _run(sync())
        _stub_esi(monkeypatch, bpos=[HULL_BPO])
        r = _plan(_client())
        sid = cp.MASS_PRODUCTION
        assert "from ESI" in r.text
        assert f'placeholder="4"' in r.text and f'id="cp-skill-{sid}"' in r.text
        assert "No synced skills" not in r.text
        r = _plan(_client(), skills_json=json.dumps({str(sid): 2}))
        assert 'value="2"' in r.text and "manual" in r.text
    finally:
        teardown()


# ── _owned ───────────────────────────────────────────────────────────────────

def _row(**kw):
    row = {"item_id": 1, "type_id": HULL_BP, "quantity": -1, "location_flag": "Hangar",
           "location_id": 60003760, "material_efficiency": 5, "time_efficiency": 10,
           "runs": -1}
    row.update(kw)
    return row


def test_owned_original_copy_and_stack():
    bpo = copy_packs._owned(_row(), "A", True)
    assert bpo.is_bpo and bpo.runs is None and (bpo.me, bpo.te) == (5, 10)
    bpc = copy_packs._owned(_row(quantity=-2, runs=7), "A", True)
    assert not bpc.is_bpo and bpc.runs == 7
    assert copy_packs._owned(_row(quantity=3), "A", True).is_bpo


def test_owned_keeps_location_id_only_for_station_flags():
    assert copy_packs._owned(_row(), "A", True).location_id == 60003760
    for flag in ("Deliveries", "CorpDeliveries"):
        assert copy_packs._owned(_row(location_flag=flag), "A", True).location_id == 60003760
    for flag in ("CorpSAG3", "Unlocked", "Locked", "Cargo"):
        assert copy_packs._owned(_row(location_flag=flag), "A", True).location_id is None
    assert copy_packs._owned(_row(location_flag="CorpSAG3"), "A", True).location_label == "Corp Hangar 3"


def test_owned_malformed_row_is_none():
    assert copy_packs._owned({"type_id": 1}, "A", True) is None
    assert copy_packs._owned(_row(item_id="x"), "A", True) is None


# ── _owned_blueprints ────────────────────────────────────────────────────────

CORP_A, CORP_B = 98000001, 98000002


def _mem(cid, name, scopes, corp=CORP_A, corp_name="Alpha"):
    return Character(character_id=cid, character_name=name, user_id=USER_ID,
                     scopes=scopes, corporation_id=corp, corporation_name=corp_name)


def _gather(monkeypatch, chars, selected, personal=None, corp=None, dead=(), fail=(),
            assets=(None, "no assets"), char_assets=None, type_names=None):
    """Run _owned_blueprints with ESI faked. `personal` is {char_id: rows};
    `corp` is the (result, err) the fallback returns, or {corp_id: (r, e)};
    `assets` is what the corp-assets fallback returns (the office map source)."""
    async def fake_refresh(char, db):
        if char.character_id in fail:
            raise RuntimeError("boom")
        return "tok"

    async def fake_pages(fn, client, char_id):
        return (personal or {}).get(char_id, [])

    async def fake_fallback(scope, scope_chars, fn, corp_id, db):
        if scope == "assets":
            return assets
        res = corp[corp_id] if isinstance(corp, dict) else corp
        return res

    async def fake_dead(cs):
        return set(dead)

    async def fake_char_assets(client, char_id):
        return (char_assets or {}).get(char_id, [])

    async def fake_type_names(db, ids):
        return {i: (type_names or {}).get(i, f"Type {i}") for i in ids}

    monkeypatch.setattr(copy_packs.esi_assets, "get_character_assets", fake_char_assets)
    monkeypatch.setattr(copy_packs.sde, "type_ids_to_names", fake_type_names)
    monkeypatch.setattr(copy_packs, "refresh_token", fake_refresh)
    monkeypatch.setattr(copy_packs, "_fetch_all_pages", fake_pages)
    monkeypatch.setattr(copy_packs, "_try_api_call_with_fallback", fake_fallback)
    monkeypatch.setattr(copy_packs, "_dead_token_ids", fake_dead)
    return _run(copy_packs._owned_blueprints(None, chars, selected))


def test_gather_notes_for_missing_scope_and_failed_fetch(monkeypatch):
    me = _mem(1, "Me", "")
    other = _mem(2, "Other", perms.BLUEPRINTS)
    _b, _c, notes, _l = _gather(monkeypatch, [me, other], me)
    assert any("Me hasn't shared blueprints" in n for n in notes)
    assert not any("Other" in n for n in notes)      # only the selected one is nagged
    me2 = _mem(1, "Me", perms.BLUEPRINTS)
    _b, _c, notes, _l = _gather(monkeypatch, [me2], me2, fail={1})
    assert "Couldn't load Me's blueprints." in notes


def test_gather_dead_token_is_skipped_with_a_note(monkeypatch):
    me = _mem(1, "Me", perms.BLUEPRINTS)
    bpos, _c, notes, _l = _gather(monkeypatch, [me], me, personal={1: [_row()]}, dead={1})
    assert bpos == []
    assert "Me needs to sign in again, so their blueprints aren't counted." in notes


def test_gather_corp_failure_and_empty_corp(monkeypatch):
    me = _mem(1, "Me", perms.BLUEPRINTS + " " + perms.CORP_BLUEPRINTS)
    _b, _c, notes, _l = _gather(monkeypatch, [me], me, corp=(None, "403"))
    assert "Alpha's blueprints couldn't be loaded." in notes
    _b, _c, notes, _l = _gather(monkeypatch, [me], me, corp=([], None))
    assert not any("couldn't be loaded" in n and "Alpha" in n for n in notes)


def test_gather_note_when_nobody_in_the_corp_shares_corp_blueprints(monkeypatch):
    me = _mem(1, "Me", perms.BLUEPRINTS)
    _b, _c, notes, _l = _gather(monkeypatch, [me], me)
    assert "No character in Alpha shares corp blueprints, so its hangars aren't counted." in notes
    npc = _mem(1, "Me", perms.BLUEPRINTS, corp=1000001, corp_name="NPC")
    _b, _c, notes, _l = _gather(monkeypatch, [npc], npc)
    assert not any("shares corp blueprints" in n for n in notes)


def test_gather_reachability_and_bpc_scope(monkeypatch):
    me = _mem(1, "Me", perms.BLUEPRINTS)
    alt = _mem(2, "Alt", perms.BLUEPRINTS, corp=CORP_B, corp_name="Beta")
    personal = {
        1: [_row(item_id=10), _row(item_id=11, quantity=-2, runs=3)],
        2: [_row(item_id=20), _row(item_id=21, quantity=-2, runs=3)],
    }
    bpos, bpcs, _n, _l = _gather(monkeypatch, [me, alt], me, personal=personal)
    by_id = {b.item_id: b for b in bpos}
    assert by_id[10].reachable is True and by_id[20].reachable is False
    assert [b.item_id for b in bpcs] == [11]           # the alt's BPC is excluded


def test_gather_corp_blueprints_and_npc_corp(monkeypatch):
    me = _mem(1, "Me", perms.CORP_BLUEPRINTS)
    alt = _mem(2, "Alt", perms.CORP_BLUEPRINTS, corp=CORP_B, corp_name="Beta")
    corp = {CORP_A: ([_row(item_id=30)], None), CORP_B: ([_row(item_id=40)], None)}
    bpos, _c, _n, _l = _gather(monkeypatch, [me, alt], me, corp=corp)
    by_id = {b.item_id: b for b in bpos}
    assert by_id[30].reachable is True and by_id[40].reachable is False
    npc = _mem(1, "Me", perms.CORP_BLUEPRINTS, corp=1000001, corp_name="NPC")
    bpos, _c, _n, _l = _gather(monkeypatch, [npc], npc, corp={1000001: ([_row()], None)})
    assert bpos == []


def test_gather_location_candidates_are_per_owner(monkeypatch):
    me = _mem(1, "Me", perms.BLUEPRINTS + " " + perms.CORP_BLUEPRINTS)
    alt = _mem(2, "Alt", perms.BLUEPRINTS + " " + perms.CORP_BLUEPRINTS, corp=CORP_B,
               corp_name="Beta")
    personal = {1: [_row(item_id=10, location_id=1111)],
                2: [_row(item_id=20, location_id=2222)]}
    corp = {CORP_A: ([_row(item_id=30, location_id=3333, location_flag="CorpDeliveries"),
                      _row(item_id=31, location_id=4444, location_flag="CorpSAG1")], None),
            CORP_B: ([], None)}
    _b, _c, _n, cands = _gather(monkeypatch, [me, alt], me, personal=personal, corp=corp)
    assert cands[1111] == [me] and cands[2222] == [alt] and cands[3333] == [me]
    assert 4444 not in cands                            # office id: never looked up


def _office(item_id, structure_id):
    return {"item_id": item_id, "location_id": structure_id, "location_flag": "OfficeFolder"}


def test_gather_resolves_a_corp_office_to_its_structure(monkeypatch):
    holder = _mem(1, "Me", perms.CORP_BLUEPRINTS + " " + perms.CORP_ASSETS)
    corp = ([_row(item_id=30, location_id=4444, location_flag="CorpSAG3")], None)
    bpos, _c, _n, cands = _gather(monkeypatch, [holder], holder, corp=corp,
                                  assets=([_office(4444, 9999)], None))
    assert bpos[0].location_id == 9999
    assert bpos[0].location_label == "Corp Hangar 3"    # the division still shows
    assert cands[9999] == [holder] and 4444 not in cands


def test_gather_accepts_a_structure_id_where_esi_gives_one_instead_of_an_office(monkeypatch):
    """ESI is inconsistent: a corp BPO in a player structure's hangar can carry the
    structure id itself as location_id, while one in an NPC station carries the
    office's item id. Both must resolve."""
    holder = _mem(1, "Me", perms.CORP_BLUEPRINTS + " " + perms.CORP_ASSETS)
    corp = ([_row(item_id=30, location_id=9999, location_flag="CorpSAG2")], None)
    bpos, _c, _n, cands = _gather(monkeypatch, [holder], holder, corp=corp,
                                  assets=([_office(4444, 9999)], None))
    assert bpos[0].location_id == 9999 and cands[9999] == [holder]
    assert bpos[0].location_label == "Corp Hangar 2"


def test_gather_keeps_the_flag_label_when_the_office_cannot_be_mapped(monkeypatch):
    corp = ([_row(item_id=30, location_id=4444, location_flag="CorpSAG3")], None)
    no_scope = _mem(1, "Me", perms.CORP_BLUEPRINTS)
    bpos, _c, _n, cands = _gather(monkeypatch, [no_scope], no_scope, corp=corp,
                                  assets=([_office(4444, 9999)], None))
    assert bpos[0].location_id is None and not cands     # assets scope never granted
    holder = _mem(1, "Me", perms.CORP_BLUEPRINTS + " " + perms.CORP_ASSETS)
    for assets in ((None, "403"), ([], None), ([_office(5555, 9999)], None)):
        bpos, _c, notes, cands = _gather(monkeypatch, [holder], holder, corp=corp,
                                         assets=assets)
        assert bpos[0].location_id is None and not cands
        assert bpos[0].location_label == "Corp Hangar 3"
        assert not any("Traceback" in n for n in notes)


def test_gather_does_not_fetch_corp_assets_without_a_hangar_bpo(monkeypatch):
    holder = _mem(1, "Me", perms.CORP_BLUEPRINTS + " " + perms.CORP_ASSETS)
    asked = []

    async def boom(scope, scope_chars, fn, corp_id, db):
        asked.append(scope)
        return ([_row(item_id=30, location_id=3333, location_flag="CorpDeliveries")], None) \
            if scope == "blueprints" else (None, "unexpected")

    monkeypatch.setattr(copy_packs, "_try_api_call_with_fallback", boom)
    # A delivery-hangar BPO already has a real location_id, so no office map is needed.
    async def fake_refresh(char, db):
        return "tok"
    async def fake_dead(cs):
        return set()
    monkeypatch.setattr(copy_packs, "refresh_token", fake_refresh)
    monkeypatch.setattr(copy_packs, "_dead_token_ids", fake_dead)
    _run(copy_packs._owned_blueprints(None, [holder], holder))
    assert asked == ["blueprints"]


def test_location_names_also_look_up_bpos_offered_only_in_the_picker(monkeypatch):
    seen = {}

    async def fake_resolve(db, ids, candidates):
        seen["ids"] = set(ids)
        return {}

    monkeypatch.setattr(copy_packs, "_resolve_location_names", fake_resolve)
    chosen = cp.OwnedBlueprint(1, HULL_BP, True, 10, 20, None, location_id=111)
    other = cp.OwnedBlueprint(2, HULL_BP, True, 10, 20, None, location_id=222)
    item = SimpleNamespace(bpo=chosen, bpo_options=[chosen, other], bpcs_used=[])
    plan = SimpleNamespace(group=lambda g: [item], bpc_items=lambda: [], items={HULL: item})
    _run(copy_packs._location_names(None, plan, {}))
    assert seen["ids"] == {111, 222}


def test_bpo_picker_is_capped_so_a_long_location_name_cannot_widen_the_tree():
    """The picker's option labels carry a station or structure name, and a select
    is as wide as its longest option: uncapped it ran past the panel on a phone."""
    css = (Path(__file__).resolve().parent.parent / "static/css/site.css").read_text()
    rule = re.search(r"\.cp-tree-src\s+select[^{]*\{([^}]*)\}", css)
    assert rule and "max-width" in rule.group(1)
    src = re.search(r"\.cp-tree-src\s*\{([^}]*)\}", css)
    assert src and "min-width: 0" in src.group(1) and "max-width: 100%" in src.group(1)


PLATES_BPC = cp.OwnedBlueprint(7010, PLATES_BP, False, 9, 14, 40, owner_name="Builder",
                               location_id=None, location_label="Personal Hangar")


def test_results_lead_with_the_pack_then_the_work_lists(monkeypatch):
    teardown = _seeded_db()
    try:
        _stub_esi(monkeypatch, bpos=[HULL_BPO], bpcs=[PLATES_BPC])
        r = _plan(_client(), state_json=json.dumps({"build": {str(PLATES): True}}))
        text = r.text
        assert "Pack: 3 BPCs (2 new, 1 existing)" in text
        assert "2 × 1 runs · 10/20 · new copy" in text
        assert "1 × 40 runs · 9/14 · existing, uses 12 of 40" in text
        assert text.index("Pack:") < text.index("1 · Make copies (1)") < text.index("2 · Collect BPCs (1)")
        assert text.count("Personal Hangar") >= 1          # the place heading
    finally:
        teardown()


def _synced_skills(monkeypatch):
    """Give the planner trained skills, so the route adds no "no synced skills" note."""
    async def fake_skills(db, char, overrides):
        levels = {sid: 5 for sid, _label in cp.PLANNER_SKILLS}
        return levels, dict(levels), []
    monkeypatch.setattr(copy_packs, "_skills", fake_skills)


def test_results_fold_the_minor_sections_with_counts(monkeypatch):
    teardown = _seeded_db()
    try:
        _stub_esi(monkeypatch, bpos=[HULL_BPO], notes=["Something is off."])
        _synced_skills(monkeypatch)
        r = _plan(_client(), state_json=json.dumps({"build": {str(PLATES): True}}))
        text = r.text
        assert text.count("<details") == 3
        assert "Source elsewhere: no BPO (1)" in text
        assert "Can&#39;t copy: buy or source (0)" in text or "Can't copy: buy or source (0)" in text
        assert "Warnings (1)" in text and "Something is off." in text
    finally:
        teardown()


def test_results_without_warnings_have_no_warnings_fold(monkeypatch):
    teardown = _seeded_db()
    try:
        _stub_esi(monkeypatch, bpos=[HULL_BPO])
        _synced_skills(monkeypatch)
        text = _plan(_client()).text
        assert "Warnings (" not in text
    finally:
        teardown()


def test_pack_with_nothing_to_hand_over(monkeypatch):
    teardown = _seeded_db()
    try:
        _stub_esi(monkeypatch)                      # no BPO, no BPC
        text = _plan(_client()).text
        assert "Pack: 0 BPCs (0 new, 0 existing)" in text
        assert "Nothing to hand over yet." in text
    finally:
        teardown()


def _site_css():
    return (Path(__file__).resolve().parent.parent / "static/css/site.css").read_text()


def _phone_css():
    """(the Copy Packs phone @media block, everything before it)."""
    css = _site_css()
    start = css.rfind("@media (max-width: 768px)", 0, css.index('.cp-grid[data-step="1"]'))
    depth, end = 0, None
    for pos in range(css.index("{", start), len(css)):
        depth += {"{": 1, "}": -1}.get(css[pos], 0)
        if depth == 0:
            end = pos
            break
    return css[start:end + 1], css[:start]


def test_page_has_the_step_bar_and_footer():
    teardown = _seeded_db()
    try:
        text = _client().get("/industry/copy-packs").text
        assert 'id="cp-grid" data-step="1"' in text
        assert 'id="cp-steps"' in text and 'id="cp-stepnav"' in text
        for n in (1, 2, 3, 4):
            assert re.search(r'data-click="cpStep" data-step="%d"' % n, text)
        assert 'data-click="cpStepNav" data-dir="-1"' in text
        assert 'data-click="cpStepNav" data-dir="1"' in text
        assert "window.cpStep = " in text and "window.cpStepNav = " in text
    finally:
        teardown()


def test_phone_shows_one_step_at_a_time():
    phone, _base = _phone_css()
    assert re.search(r"\.cp-left > \.b-section, #cp-skills, #cp-tree, \.cp-right \{[^}]*display: none", phone)
    for n, sel in ((1, ".cp-left > .b-section"), (2, "#cp-skills"), (3, "#cp-tree"), (4, ".cp-right")):
        assert re.search(r'\.cp-grid\[data-step="%d"\] %s \{[^}]*display: block' % (n, re.escape(sel)), phone)
    assert "order:" not in phone.replace("border", "")      # no stacking order any more


def test_step_bar_and_footer_exist_only_on_phones():
    phone, base = _phone_css()
    assert re.search(r"\.cp-steps, \.cp-stepnav, \.cp-tree-open, \.cp-tree-up \{ display: none; \}", base)
    assert re.search(r"\.cp-steps, \.cp-stepnav \{ display: flex; \}", phone)


def test_tree_rows_carry_the_attributes_the_phone_drill_down_uses(monkeypatch):
    teardown = _seeded_db()
    try:
        _stub_esi(monkeypatch, bpos=[HULL_BPO])
        text = _plan(_client(), state_json=json.dumps({"build": {str(PLATES): True}})).text
        assert f'data-idx="0" data-parent="" data-type="{HULL}"' in text
        assert f'data-idx="1" data-parent="0" data-type="{PLATES}"' in text
        assert 'data-idx="2" data-parent="1" data-type="34"' in text
        assert text.count('data-has-kids="1"') == 2          # the hull and the plates, not Tritanium
        assert 'id="cp-tree" class="cp-drill" hx-swap-oob="true"' in text
        assert 'id="cp-tree-up"' in text and text.count('data-click="cpTreeOpen"') == 2
    finally:
        teardown()


def test_tree_without_children_has_no_open_controls(monkeypatch):
    teardown = _seeded_db()
    try:
        _stub_esi(monkeypatch, bpos=[HULL_BPO])
        text = _plan(_client()).text                         # plates left on Buy
        assert text.count('data-has-kids="1"') == 1          # only the hull
    finally:
        teardown()


def test_phone_tree_shows_only_the_focused_row_and_its_children():
    phone, base = _phone_css()
    assert re.search(r"#cp-tree\.cp-drill \.cp-tree-row:not\(\.cp-vis\) \{[^}]*display: none", phone)
    assert re.search(r"\.cp-tree-open \{[^}]*display: inline-block", phone)
    assert re.search(r"\.cp-tree-up:not\(\[hidden\]\) \{[^}]*display: block", phone)
    assert re.search(r"#cp-tree \.cp-tree-row \{[^}]*padding-left: 0\.5rem !important", phone)


def test_page_script_has_the_tree_handlers():
    teardown = _seeded_db()
    try:
        text = _client().get("/industry/copy-packs").text
        assert "window.cpTreeOpen = " in text and "window.cpTreeUp = " in text
        assert "new MutationObserver(applyTreeFocus)" in text
    finally:
        teardown()


def test_phone_edge_cases_found_in_review():
    teardown = _seeded_db()
    try:
        text = _client().get("/industry/copy-packs").text
        # A failed plan lands in Results, which is hidden on steps 1-3: go there.
        assert "if (id === 'cp-form') setStep(4);" in text
        # Steps 2 and 3 are not blank before a product is picked.
        assert '<div id="cp-skills"><div class="b-section cp-phone-only"><div class="b-empty">Pick a product first.' in text
        # The placeholder already has the drill class, so the first plan does not flash the full tree.
        assert '<div id="cp-tree" class="cp-drill"><div class="b-section cp-phone-only"><div class="b-empty">Pick a product first.' in text
        # No throw if a tree row has no name text.
        assert "firstChild.textContent" not in text
    finally:
        teardown()
    _phone, base = _phone_css()
    assert re.search(r"\.cp-tree-open \{[^}]*min-height: 32px", base)
    phone, base = _phone_css()                      # the placeholders are for phones only
    assert re.search(r"\.cp-phone-only \{ display: none; \}", base)
    assert re.search(r"\.cp-phone-only \{ display: block; \}", phone)


# ── cache TTL ────────────────────────────────────────────────────────────────

def test_corp_blueprints_cache_for_an_hour():
    assert _ttl_for_path("/corporations/98000001/blueprints/") == TTL["blueprints"] == 3600
    assert _ttl_for_path("/corporations/98000001/") == TTL["corporation"]
    assert _ttl_for_path("/characters/123/blueprints/") == 300      # unchanged


def test_shared_build_item_gets_its_input_ids_once(monkeypatch):
    """An item under two parents appears in two tree rows; only the first
    (expanded) row may carry the ME/TE ids."""
    teardown = _seeded_db()
    try:
        shared, shared_bp, mid, mid_bp = 99101, 99102, 99103, 99104

        async def add():
            async with teardown.session() as db:
                db.add(SDEType(type_id=shared, type_name="Shared Part", published=True))
                db.add(SDEType(type_id=mid, type_name="Mid Part", published=True))
                for bp, prod in ((shared_bp, shared), (mid_bp, mid)):
                    db.add(SDEBlueprintInfo(blueprint_type_id=bp, product_type_id=prod,
                                            product_quantity=1, manufacturing_time=100,
                                            max_production_limit=10, copying_time=100))
                db.add(SDEBlueprintMaterial(blueprint_type_id=shared_bp, activity_id=1,
                                            material_type_id=34, quantity=5))
                db.add(SDEBlueprintMaterial(blueprint_type_id=mid_bp, activity_id=1,
                                            material_type_id=shared, quantity=2))
                db.add(SDEBlueprintMaterial(blueprint_type_id=HULL_BP, activity_id=1,
                                            material_type_id=mid, quantity=3))
                db.add(SDEBlueprintMaterial(blueprint_type_id=PLATES_BP, activity_id=1,
                                            material_type_id=shared, quantity=1))
                await db.commit()
        _run(add())
        _stub_esi(monkeypatch, bpos=[HULL_BPO])
        state = {"build": {str(PLATES): True, str(mid): True, str(shared): True}}
        r = _plan(_client(), state_json=json.dumps(state))
        assert r.status_code == 200
        assert r.text.count(f'id="cp-me-{shared}"') == 1
        assert r.text.count(f'id="cp-te-{shared}"') == 1
    finally:
        teardown()


def test_gather_flags_lists_cut_off_at_the_page_cap(monkeypatch):
    me = _mem(1, "Me", perms.BLUEPRINTS + " " + perms.CORP_BLUEPRINTS)
    full = [_row(item_id=i) for i in range(10_000)]
    _b, _c, notes, _l = _gather(monkeypatch, [me], me, personal={1: full},
                                corp=(full, None))
    assert "Me's blueprint list may be incomplete (over 10,000 blueprints)." in notes
    assert "Alpha's blueprint list may be incomplete (over 10,000 blueprints)." in notes
    _b, _c, notes, _l = _gather(monkeypatch, [me], me, personal={1: full[:9_999]},
                                corp=(full[:9_999], None))
    assert not any("incomplete" in n for n in notes)


def test_search_box_opts_out_of_the_error_pill(monkeypatch):
    teardown = _seeded_db()
    try:
        r = _client().get("/industry/copy-packs")
        assert 'id="cp-search"' in r.text
        assert "Search failed. Try again." in r.text
        assert r.text.count('data-htmx-no-error="1"') >= 2
    finally:
        teardown()


def test_plan_shows_structure_and_division_for_a_corp_bpo(monkeypatch):
    teardown = _seeded_db()
    try:
        corp_bpo = cp.OwnedBlueprint(7002, HULL_BP, True, 10, 20, None,
                                     owner_name="Alpha Corp",
                                     location_id=9999, location_label="Corp Hangar 3")
        _stub_esi(monkeypatch, bpos=[corp_bpo])

        async def fake_locations(db, plan, candidates):
            return {9999: "Azbel Keepstar"}
        monkeypatch.setattr(copy_packs, "_location_names", fake_locations)
        r = _plan(_client())
        assert "Alpha Corp, Azbel Keepstar, Corp Hangar 3" in r.text

        personal_bpo = cp.OwnedBlueprint(7003, HULL_BP, True, 10, 20, None,
                                         owner_name="Builder",
                                         location_id=9999, location_label="Personal Hangar")
        _stub_esi(monkeypatch, bpos=[personal_bpo])
        monkeypatch.setattr(copy_packs, "_location_names", fake_locations)
        r = _plan(_client())
        assert "Builder, Azbel Keepstar" in r.text
        assert "Azbel Keepstar, Personal Hangar" not in r.text
    finally:
        teardown()


def test_plan_bpo_dropdown_options_include_the_location(monkeypatch):
    teardown = _seeded_db()
    try:
        bpos = [
            cp.OwnedBlueprint(7004, HULL_BP, True, 10, 20, None,
                              owner_name="Alpha Corp",
                              location_id=None, location_label="Corp Hangar 2"),
            cp.OwnedBlueprint(7005, HULL_BP, True, 10, 20, None,
                              owner_name="Alpha Corp",
                              location_id=9999, location_label="Corp Hangar 3"),
        ]
        _stub_esi(monkeypatch, bpos=bpos)

        async def fake_locations(db, plan, candidates):
            return {9999: "Azbel Keepstar"}
        monkeypatch.setattr(copy_packs, "_location_names", fake_locations)
        r = _plan(_client())
        assert "BPO 10/20 · Alpha Corp, Corp Hangar 2" in r.text
        assert "BPO 10/20 · Alpha Corp, Azbel Keepstar, Corp Hangar 3" in r.text
    finally:
        teardown()


# ── blueprints inside ships and containers ───────────────────────────────────

SHIP, STATION, CONTAINER = 9001, 60003760, 9002


def _asset(item_id, type_id, location_id, kind="station", flag="Hangar"):
    return {"item_id": item_id, "type_id": type_id, "location_id": location_id,
            "location_type": kind, "location_flag": flag}


def test_resolve_holder_walks_up_to_the_station():
    assets = {SHIP: _asset(SHIP, 587, STATION),
              CONTAINER: _asset(CONTAINER, 3293, SHIP, "item", "Cargo")}
    assert copy_packs.resolve_holder(SHIP, assets) == (STATION, 587, False)
    # A container inside the ship: the label is the container, the place the station.
    assert copy_packs.resolve_holder(CONTAINER, assets) == (STATION, 3293, False)
    # A structure reads as location_type "other" and is a place too.
    assert copy_packs.resolve_holder(SHIP, {SHIP: _asset(SHIP, 587, 1234567, "other")}) == (1234567, 587, False)


def test_resolve_holder_gives_the_system_for_a_ship_in_space():
    system = 30000142
    assert copy_packs.resolve_holder(SHIP, {SHIP: _asset(SHIP, 587, system, "solar_system", "AutoFit")}) == (system, 587, True)
    # A container in a ship in space: the label is the container, the place the ship's system.
    nested = {SHIP: _asset(SHIP, 587, system, "solar_system", "AutoFit"),
              CONTAINER: _asset(CONTAINER, 3293, SHIP, "item", "Cargo")}
    assert copy_packs.resolve_holder(CONTAINER, nested) == (system, 3293, True)


def test_resolve_holder_gives_no_place_when_unlisted_or_looping():
    assert copy_packs.resolve_holder(SHIP, {}) == (None, None, False)
    # A cycle or a chain that never reaches a place stops instead of looping.
    loop = {SHIP: _asset(SHIP, 587, CONTAINER, "item"), CONTAINER: _asset(CONTAINER, 3293, SHIP, "item")}
    assert copy_packs.resolve_holder(SHIP, loop) == (None, 587, False)


def test_owned_remembers_the_item_a_blueprint_sits_in():
    assert copy_packs._owned(_row(location_flag="Cargo", location_id=SHIP), "A", True).holder_id == SHIP
    assert copy_packs._owned(_row(location_flag="Unlocked", location_id=CONTAINER), "A", True).holder_id == CONTAINER
    assert copy_packs._owned(_row(), "A", True).holder_id is None
    assert copy_packs._owned(_row(location_flag="CorpSAG3"), "A", True).holder_id is None


def test_gather_places_a_cargo_bpc_in_its_ship_and_station(monkeypatch):
    me = _mem(1, "Me", perms.BLUEPRINTS + " " + perms.ASSETS)
    personal = {1: [_row(item_id=11, quantity=-2, runs=3, location_flag="Cargo", location_id=SHIP)]}
    _b, bpcs, notes, cands = _gather(
        monkeypatch, [me], me, personal=personal,
        char_assets={1: [_asset(SHIP, 587, STATION)]}, type_names={587: "Rifter"})
    assert (bpcs[0].location_id, bpcs[0].holder_label) == (STATION, "Rifter")
    assert not any("couldn't be located" in n for n in notes)
    assert 1 in [c.character_id for c in cands[STATION]]    # its name can be looked up


def test_gather_cargo_without_the_assets_scope_keeps_the_flag_and_says_so(monkeypatch):
    me = _mem(1, "Me", perms.BLUEPRINTS)
    personal = {1: [_row(item_id=11, quantity=-2, runs=3, location_flag="Cargo", location_id=SHIP)]}
    _b, bpcs, notes, _c = _gather(monkeypatch, [me], me, personal=personal)
    assert bpcs[0].holder_label == "" and bpcs[0].location_label == "Cargo"
    assert any("Me has 1 blueprint inside a ship or container" in n for n in notes)


def test_gather_corp_bpc_in_a_ship_uses_the_corp_assets(monkeypatch):
    me = _mem(1, "Me", perms.CORP_BLUEPRINTS + " " + perms.CORP_ASSETS)
    corp = ([_row(item_id=31, quantity=-2, runs=4, location_flag="Cargo", location_id=SHIP)], None)
    _b, bpcs, _n, _c = _gather(monkeypatch, [me], me, corp=corp,
                               assets=([_asset(SHIP, 587, STATION)], None),
                               type_names={587: "Rifter"})
    assert (bpcs[0].owner_name, bpcs[0].location_id, bpcs[0].holder_label) == ("Alpha", STATION, "Rifter")


def test_collect_header_names_owner_ship_and_station(monkeypatch):
    teardown = _seeded_db()
    try:
        cargo_bpc = cp.OwnedBlueprint(7100, PLATES_BP, False, 10, 16, 40, owner_name="Builder",
                                      location_id=STATION, location_label="Cargo",
                                      holder_id=SHIP, holder_type_id=587, holder_label="Rifter")
        _stub_esi(monkeypatch, bpos=[HULL_BPO], bpcs=[cargo_bpc])

        async def names(db, plan, candidates):
            return {STATION: "Faurent I"}

        monkeypatch.setattr(copy_packs, "_location_names", names)
        r = _plan(_client(), state_json=json.dumps({"build": {str(PLATES): True}}))
        assert r.status_code == 200
        assert "Rifter in Faurent I" in r.text
        assert "· Builder (1)" in r.text
        assert ">Cargo " not in r.text      # no longer a bare "Cargo" group
    finally:
        teardown()


def test_gather_marks_a_cargo_bpc_in_a_ship_in_space(monkeypatch):
    me = _mem(1, "Me", perms.BLUEPRINTS + " " + perms.ASSETS)
    personal = {1: [_row(item_id=11, quantity=-2, runs=3, location_flag="Cargo", location_id=SHIP)]}
    _b, bpcs, _n, _c = _gather(
        monkeypatch, [me], me, personal=personal,
        char_assets={1: [_asset(SHIP, 587, 30000142, "solar_system", "AutoFit")]},
        type_names={587: "Rifter"})
    assert (bpcs[0].location_id, bpcs[0].holder_label, bpcs[0].holder_in_space) == (30000142, "Rifter", True)


def test_collect_header_names_the_system_for_a_ship_in_space(monkeypatch):
    teardown = _seeded_db()
    try:
        bpc = cp.OwnedBlueprint(7101, PLATES_BP, False, 10, 16, 40, owner_name="Builder",
                                location_id=30000142, location_label="Cargo", holder_id=SHIP,
                                holder_type_id=587, holder_label="Rifter", holder_in_space=True)
        _stub_esi(monkeypatch, bpos=[HULL_BPO], bpcs=[bpc])

        async def names(db, plan, candidates):
            return {30000142: "Jita"}

        monkeypatch.setattr(copy_packs, "_location_names", names)
        r = _plan(_client(), state_json=json.dumps({"build": {str(PLATES): True}}))
        assert r.status_code == 200
        assert "Rifter (in space, Jita)" in r.text
        assert "location unknown" not in r.text
    finally:
        teardown()
