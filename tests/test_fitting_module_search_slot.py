"""Regression test for ISS-059: GET /tools/fitting/search/modules?slot=med
returned every slot's modules instead of filtering to mid-slot ones.

app/routes/fitting.py's search_modules() only allowed slot="mid" through its
allow-list, but SDEModuleSlot.slot_type (and the fitting tool's own
data-slot="med" search box, and app/sde/loader.py's SLOT_EFFECT_MAP) all use
"med", never "mid". So a mid-slot search's slot_filter always fell through to
None and sde.search_modules() (app/sde/lookup.py) returned unfiltered
results, while the high/low/rig/subsystem boxes filtered correctly because
their keys matched the allow-list.

Seeds a real mid-slot "Warp Scrambler" and a low-slot "Warp Core Stabilizer"
— two modules that share the "Warp" substring but sit in different slots —
and asserts slot=med and slot=mid each return only the mid-slot one, slot=low
returns only the low-slot one, and no slot / an unknown slot return both.
"""
import asyncio
import base64
import json
import tempfile

import itsdangerous
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.db.sde_models as sm
from app.db.models import Base, User, get_db

USER_A = 8901
_CSRF = "test-csrf-token-modsearchslot-0123"

# category_id 7 == CATEGORY_MODULE (app/sde/lookup.py)
GROUP_MODULES = 9700

MID_MOD = 98101    # "Warp Scrambler" — mid slot
LOW_MOD = 98102    # "Warp Core Stabilizer" — low slot


def _authed_client():
    """Signed session cookie carrying user_id AND the csrf token the
    middleware checks the X-CSRF-Token header against — the search routes
    are login-only since ISS-044."""
    import app.main as main
    signer = itsdangerous.TimestampSigner(main.settings.secret_key)
    payload = {"user_id": USER_A, "csrf_token": _CSRF}
    cookie = signer.sign(base64.b64encode(json.dumps(payload).encode())).decode()
    client = TestClient(main.app, base_url="https://testserver")
    client.cookies.set("vigilant_session", cookie)
    return client


async def _seed(db):
    db.add(sm.SDEGroup(group_id=GROUP_MODULES, category_id=7, group_name="Test Modules"))
    db.add_all([
        sm.SDEType(type_id=MID_MOD, type_name="Warp Scrambler", group_id=GROUP_MODULES,
                   category_id=7, published=True),
        sm.SDEType(type_id=LOW_MOD, type_name="Warp Core Stabilizer", group_id=GROUP_MODULES,
                   category_id=7, published=True),
    ])
    db.add_all([
        sm.SDEModuleSlot(type_id=MID_MOD, slot_type="med", is_turret=False, is_launcher=False),
        sm.SDEModuleSlot(type_id=LOW_MOD, slot_type="low", is_turret=False, is_launcher=False),
    ])


def _seeded_db():
    """Temp-file sqlite DB seeded with the two Warp-named modules above;
    returns teardown()."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

    async def _setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as db:
            db.add(User(id=USER_A))
            await db.commit()
        async with SessionLocal() as db:
            await _seed(db)
            await db.commit()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_setup())
    finally:
        loop.close()
        asyncio.set_event_loop(None)

    async def override_get_db():
        async with SessionLocal() as session:
            yield session

    import app.main as main
    main.app.dependency_overrides[get_db] = override_get_db

    def teardown():
        main.app.dependency_overrides.pop(get_db, None)

    return teardown


def _names_from_html(response):
    # The route renders an HTML partial (fitting_search_results.html), not
    # JSON — just check which module names appear in the fragment.
    text = response.text
    return {name for name in ("Warp Scrambler", "Warp Core Stabilizer") if name in text}


def test_slot_med_returns_only_the_mid_slot_module():
    teardown = _seeded_db()
    try:
        client = _authed_client()
        r = client.get("/tools/fitting/search/modules?q=Warp&slot=med")
        assert r.status_code == 200
        assert _names_from_html(r) == {"Warp Scrambler"}
    finally:
        teardown()


def test_slot_mid_is_accepted_as_an_alias_for_med():
    teardown = _seeded_db()
    try:
        client = _authed_client()
        r = client.get("/tools/fitting/search/modules?q=Warp&slot=mid")
        assert r.status_code == 200
        assert _names_from_html(r) == {"Warp Scrambler"}
    finally:
        teardown()


def test_slot_low_returns_only_the_low_slot_module():
    teardown = _seeded_db()
    try:
        client = _authed_client()
        r = client.get("/tools/fitting/search/modules?q=Warp&slot=low")
        assert r.status_code == 200
        assert _names_from_html(r) == {"Warp Core Stabilizer"}
    finally:
        teardown()


def test_no_slot_returns_both():
    teardown = _seeded_db()
    try:
        client = _authed_client()
        r = client.get("/tools/fitting/search/modules?q=Warp")
        assert r.status_code == 200
        assert _names_from_html(r) == {"Warp Scrambler", "Warp Core Stabilizer"}
    finally:
        teardown()


def test_unknown_slot_returns_both():
    teardown = _seeded_db()
    try:
        client = _authed_client()
        r = client.get("/tools/fitting/search/modules?q=Warp&slot=bogus")
        assert r.status_code == 200
        assert _names_from_html(r) == {"Warp Scrambler", "Warp Core Stabilizer"}
    finally:
        teardown()


def test_search_requires_a_session():
    teardown = _seeded_db()
    try:
        import app.main as main
        client = TestClient(main.app, base_url="https://testserver")
        r = client.get("/tools/fitting/search/modules?q=Warp&slot=med")
        assert r.status_code == 401
    finally:
        teardown()
