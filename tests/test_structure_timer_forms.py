"""Structure Timers: what the Edit form sends back.

ISS-128a: the Edit form's phase select offered only Shield, Armor and Hull,
while the Add form, the routes and the ESI sync also use Anchoring and
Unanchoring. A select whose value is missing from its options submits its
first option, so saving any edit to an anchoring or unanchoring timer
silently turned it into a Shield timer. The round-trip test builds the POST
body from the rendered form the way a browser does, then checks the phase
survives.

Names and ids are invented."""
import asyncio
import base64
import json
import tempfile
import types
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

import itsdangerous
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, StructureTimer, User, get_db
from app.routes import structure_timers as st_mod
from tests._mobile import render_page

_NS = types.SimpleNamespace

PHASES = ["shield", "armor", "hull", "anchoring", "unanchoring"]
USER_ID = 4101
CSRF = "test-csrf-token"


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _FormFields(HTMLParser):
    """For the form posting to `action`: the (name, value) pairs a browser
    submits, and each select's options as (value, label, selected).

    Submitted: text and hidden inputs, checked radios and checkboxes, each
    select's selected option (its first option when none is marked
    selected), and textarea text."""

    def __init__(self, action):
        super().__init__(convert_charrefs=True)
        self.action = action
        self.found = 0
        self.inside = False
        self.fields = []
        self.selects = {}
        self._select = None
        self._option = None
        self._textarea = None

    def handle_starttag(self, tag, attrs):
        a = {k: (v if v is not None else "") for k, v in attrs}
        if tag == "form":
            self.inside = a.get("action") == self.action
            self.found += self.inside
            return
        if not self.inside:
            return
        if tag == "input":
            kind = a.get("type", "text")
            if "name" not in a or kind in ("submit", "button", "reset", "image", "file"):
                return
            if kind in ("radio", "checkbox"):
                if "checked" in a:
                    self.fields.append((a["name"], a.get("value", "on")))
                return
            self.fields.append((a["name"], a.get("value", "")))
        elif tag == "select":
            self._select = a.get("name")
            self.selects[self._select] = []
        elif tag == "option" and self._select is not None:
            assert "value" in a, "every option here carries a value"
            self._option = [a["value"], "", "selected" in a]
            self.selects[self._select].append(self._option)
        elif tag == "textarea":
            self._textarea = [a.get("name"), ""]

    def handle_data(self, data):
        if self._option is not None:
            self._option[1] += data
        if self._textarea is not None:
            self._textarea[1] += data

    def handle_endtag(self, tag):
        if tag == "form":
            self.inside = False
        elif tag == "option":
            self._option = None
        elif tag == "select" and self._select is not None:
            opts = self.selects[self._select]
            chosen = [o for o in opts if o[2]] or opts[:1]
            if chosen:
                self.fields.append((self._select, chosen[-1][0]))
            self._select = None
        elif tag == "textarea" and self._textarea is not None:
            name, text = self._textarea
            if name:
                self.fields.append((name, text[1:] if text.startswith("\n") else text))
            self._textarea = None


def _form(html, action):
    p = _FormFields(action)
    p.feed(html)
    assert p.found == 1, f"expected one form posting to {action}, found {p.found}"
    p.selects = {k: [(v, label.strip(), sel) for v, label, sel in opts]
                 for k, opts in p.selects.items()}
    return p


# ── the rendered Edit form ────────────────────────────────────────────


def _timer(tid, phase, expires=datetime(2030, 1, 2, 3, 4, 5)):
    return _NS(id=tid, structure_name=f"Sample Structure {tid}", structure_type="athanor",
               system_name="Sample-01", region_name="Sample Region", owner_name="Sample Corp",
               disposition="hostile", timer_phase=phase, priority="normal", timer_expires=expires,
               notes=None, source="manual", created_by=1, acl_group_id=None)


def _render(timers):
    return render_page(st_mod, "structure_timers.html", "/structure-timers",
                       active_timers=list(timers), archived_timers=[], acl_groups=[],
                       user_id=1, is_privileged=False)


@pytest.mark.parametrize("phase", PHASES)
def test_edit_form_offers_the_add_forms_phases_and_selects_the_timers_own(phase):
    html = _render([_timer(31, phase)])
    add = _form(html, "/structure-timers/create").selects["timer_phase"]
    edit = _form(html, "/structure-timers/31/edit").selects["timer_phase"]
    assert [(v, label) for v, label, _ in edit] == [(v, label) for v, label, _ in add]
    assert [v for v, _, _ in edit] == PHASES
    assert [v for v, _, sel in edit if sel] == [phase]


# ── the round trip through the routes ─────────────────────────────────


def _client():
    import app.main as main
    signer = itsdangerous.TimestampSigner(main.settings.secret_key)
    cookie = signer.sign(base64.b64encode(
        json.dumps({"user_id": USER_ID, "csrf_token": CSRF}).encode())).decode()
    c = TestClient(main.app, base_url="https://testserver")
    c.cookies.set("vigilant_session", cookie)
    c.headers.update({"X-CSRF-Token": CSRF})
    return c


@pytest.fixture
def timers_db():
    import app.main as main

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
    # Two days out: the page archives a timer an hour past its expiry.
    expires = (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=2)).replace(second=0, microsecond=0)

    async def seed():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with SessionLocal() as db:
            db.add(User(id=USER_ID, role="user"))
            for tid, phase in enumerate(PHASES, start=51):
                db.add(StructureTimer(
                    id=tid, structure_name=f"Sample Structure {tid}", structure_type="athanor",
                    system_name="Sample-01", region_name="Sample Region",
                    owner_name="Sample Corp", disposition="friendly", timer_phase=phase,
                    timer_expires=expires, priority="critical", notes="Sample note",
                    source="manual", created_by=USER_ID))
            await db.commit()
    _run(seed())

    async def override():
        async with SessionLocal() as s:
            yield s
    main.app.dependency_overrides[get_db] = override
    yield SessionLocal
    main.app.dependency_overrides.pop(get_db, None)
    _run(engine.dispose())


def _stored(SessionLocal, tid):
    async def go():
        async with SessionLocal() as s:
            return (await s.execute(select(StructureTimer).where(StructureTimer.id == tid))).scalar_one()
    return _run(go())


@pytest.mark.parametrize("phase", PHASES)
def test_saving_the_edit_form_unchanged_keeps_the_phase(timers_db, phase):
    tid = 51 + PHASES.index(phase)
    before = _stored(timers_db, tid)
    client = _client()
    page = client.get("/structure-timers", follow_redirects=False)
    assert page.status_code == 200, page.text[:300]
    fields = _form(page.text, f"/structure-timers/{tid}/edit").fields
    names = [name for name, _ in fields]
    assert len(names) == len(set(names)), names
    body = dict(fields)
    r = client.post(f"/structure-timers/{tid}/edit", data=body, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/structure-timers"
    after = _stored(timers_db, tid)
    assert after.timer_phase == phase, f"the form sent timer_phase={body.get('timer_phase')!r}"
    for col in ("structure_name", "structure_type", "system_name", "region_name", "owner_name",
                "disposition", "priority", "notes", "timer_expires", "acl_group_id"):
        assert getattr(after, col) == getattr(before, col), col
