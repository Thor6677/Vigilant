"""The alliance routes remember what ESI said, misses included (ISS-178).

`/api/map/alliance/{id}` asked ESI's /alliances/{id}/ on every view, valid
ids included, and `/api/map/alliances?ids=` took any number of ids and never
remembered the ones ESI had no alliance for (the placeholder went only into
the response). A failed lookup spends the ESI error budget the whole instance
shares, so a repeated bad id cost an error every time.

Now the detail route keeps the header (a 200 for a day, ESI's "no such id"
for ten minutes), and the names route takes each id once, at most
MAP_ALLIANCES_MAX_IDS per request, and remembers for ten minutes the ids ESI
answered as unknown, without putting the placeholder in the expiry-less name
cache.

A miss is remembered only for an id ESI itself answered as unknown, and only
under that id. A throttle, a 5xx, a 403 or a network failure is never kept as
"no such alliance", and neither is a refused multi-id batch: ESI refuses a
whole batch for one bad id, so a bad id sent beside real ones must not blank
the real ones for everyone else.

ESI is a stub httpx client that records each call; the clock is the module's
`time`. Ids and names are invented.
"""
import asyncio
import json
import tempfile
import types
from collections import OrderedDict

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.db.models as models
import app.db.sde_models  # noqa: F401  (registers the SDE tables)
from app.db.models import Base
from app.routes import starmap as starmap_mod

_NS = types.SimpleNamespace

KNOWN = 99178001
UNKNOWN = 99178999

_HEADER = {"name": "Sample Alliance", "ticker": "SMPL", "creator_id": 90000001,
           "executor_corporation_id": 98000001, "date_founded": "2020-01-02T03:04:05Z",
           "faction_id": None}


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _Resp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


@pytest.fixture
def esi(monkeypatch):
    """A recording stand-in for ESI, a temp DB, a settable clock, and empty caches.

    `esi.get_answer(path)` and `esi.post_answer(ids)` decide each response;
    returning an Exception instance raises it."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp.name}")

    async def schema():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    _run(schema())
    monkeypatch.setattr(models, "AsyncSessionLocal",
                        async_sessionmaker(engine, expire_on_commit=False))

    state = _NS(gets=[], posts=[], clock=[1000.0],
                get_answer=lambda path: _Resp(200, _HEADER),
                post_answer=lambda ids: _Resp(200, []))

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, path):
            state.gets.append(path)
            answer = state.get_answer(path)
            if isinstance(answer, Exception):
                raise answer
            return answer

        async def post(self, path, json=None):
            state.posts.append(list(json))
            answer = state.post_answer(list(json))
            if isinstance(answer, Exception):
                raise answer
            return answer

    monkeypatch.setattr(starmap_mod, "httpx", _NS(AsyncClient=_Client))
    monkeypatch.setattr(starmap_mod, "time", _NS(monotonic=lambda: state.clock[0]),
                        raising=False)
    monkeypatch.setattr(starmap_mod, "_alliance_cache", {})
    monkeypatch.setattr(starmap_mod, "_alliance_detail_cache", OrderedDict(), raising=False)
    monkeypatch.setattr(starmap_mod, "_alliance_name_misses", OrderedDict(), raising=False)
    monkeypatch.setattr(starmap_mod, "_stats_cache", {})
    yield state
    _run(engine.dispose())


def _detail(alliance_id):
    resp = _run(starmap_mod.alliance_detail(alliance_id, _NS(session={"user_id": 1})))
    assert resp.status_code == 200
    return json.loads(resp.body)


def _names(ids_param):
    req = _NS(session={"user_id": 1}, query_params={"ids": ids_param})
    resp = _run(starmap_mod.map_alliances(req))
    assert resp.status_code == 200
    return json.loads(resp.body)


# ── /api/map/alliance/{id} ────────────────────────────────────────────

def test_detail_asks_esi_once_for_a_known_alliance(esi):
    first = _detail(KNOWN)
    second = _detail(KNOWN)
    assert esi.gets == [f"/alliances/{KNOWN}/"]
    for body in (first, second):
        assert (body["name"], body["ticker"], body["date_founded"]) == (
            "Sample Alliance", "SMPL", "2020-01-02T03:04:05Z")
        assert body["alliance_id"] == KNOWN


def test_detail_refetches_once_the_header_expires(esi):
    _detail(KNOWN)
    esi.clock[0] += starmap_mod.ALLIANCE_DETAIL_TTL - 1
    _detail(KNOWN)
    assert len(esi.gets) == 1
    esi.clock[0] += 2
    _detail(KNOWN)
    assert len(esi.gets) == 2


@pytest.mark.parametrize("status", [404, 400, 422])
def test_detail_remembers_an_unknown_alliance_briefly(esi, status):
    esi.get_answer = lambda path: _Resp(status, {"error": "not found"})
    for _ in range(3):
        body = _detail(UNKNOWN)
        assert "name" not in body and "ticker" not in body
        assert body["alliance_id"] == UNKNOWN
    assert esi.gets == [f"/alliances/{UNKNOWN}/"]

    esi.clock[0] += starmap_mod.ALLIANCE_MISS_TTL + 1
    _detail(UNKNOWN)
    assert len(esi.gets) == 2


_TRANSIENT = [_Resp(503), _Resp(502), _Resp(500), _Resp(420), _Resp(429), _Resp(403),
              httpx.ReadTimeout("timed out"), RuntimeError("connection reset")]


@pytest.mark.parametrize("answer", _TRANSIENT)
def test_detail_does_not_remember_a_transient_failure(esi, answer):
    esi.get_answer = lambda path: answer
    _detail(KNOWN)
    _detail(KNOWN)
    assert len(esi.gets) == 2
    assert KNOWN not in starmap_mod._alliance_detail_cache

    esi.get_answer = lambda path: _Resp(200, _HEADER)
    assert _detail(KNOWN)["name"] == "Sample Alliance"


def test_detail_cache_is_bounded(esi, monkeypatch):
    monkeypatch.setattr(starmap_mod, "ALLIANCE_LOOKUP_ENTRIES", 3)
    esi.get_answer = lambda path: _Resp(404)
    for aid in range(99100001, 99100006):
        _detail(aid)
    assert list(starmap_mod._alliance_detail_cache) == [99100003, 99100004, 99100005]


# ── /api/map/alliances?ids= ───────────────────────────────────────────

def test_names_remember_an_unresolved_id_as_a_placeholder(esi):
    """ESI answers 200 without the id: the placeholder is served again
    without asking, but never lands in the expiry-less name cache."""
    assert _names(str(UNKNOWN)) == {str(UNKNOWN): f"Alliance {UNKNOWN}"}
    assert _names(str(UNKNOWN)) == {str(UNKNOWN): f"Alliance {UNKNOWN}"}
    assert esi.posts == [[UNKNOWN]]
    assert UNKNOWN not in starmap_mod._alliance_cache

    esi.clock[0] += starmap_mod.ALLIANCE_MISS_TTL + 1
    _names(str(UNKNOWN))
    assert esi.posts == [[UNKNOWN], [UNKNOWN]]


@pytest.mark.parametrize("status", [404, 400, 422])
def test_names_remember_a_lone_id_esi_refused(esi, status):
    """A one-id batch refused as invalid is ESI's answer about that id."""
    esi.post_answer = lambda ids: _Resp(status, {"error": "Ensure all IDs are valid"})
    assert _names(str(UNKNOWN)) == {str(UNKNOWN): f"Alliance {UNKNOWN}"}
    assert _names(str(UNKNOWN)) == {str(UNKNOWN): f"Alliance {UNKNOWN}"}
    assert esi.posts == [[UNKNOWN]]
    assert list(starmap_mod._alliance_name_misses) == [UNKNOWN]


def _esi_refuses_batches_with(bad):
    """ESI's /universe/names/: one invalid id refuses the whole batch."""
    def answer(ids):
        if bad in ids:
            return _Resp(404, {"error": "Ensure all IDs are valid before resolving"})
        return _Resp(200, [{"id": i, "name": f"Sample Alliance {i}", "category": "alliance"}
                           for i in ids])
    return answer


def test_a_bad_id_beside_real_ones_does_not_blank_them(esi):
    """One user's request mixing a bad id with real ones gets placeholders
    for all of them, but nothing is remembered: the next request for the
    real ids resolves them."""
    real = [99178101, 99178102]
    esi.post_answer = _esi_refuses_batches_with(UNKNOWN)
    body = _names(",".join(map(str, [real[0], UNKNOWN, real[1]])))
    assert body == {str(i): f"Alliance {i}" for i in (real[0], UNKNOWN, real[1])}
    assert not starmap_mod._alliance_name_misses

    assert _names(",".join(map(str, real))) == {
        str(i): f"Sample Alliance {i}" for i in real}
    assert esi.posts == [[real[0], UNKNOWN, real[1]], real]


def test_a_refused_multi_id_batch_is_retried(esi):
    esi.post_answer = _esi_refuses_batches_with(UNKNOWN)
    _names(f"{UNKNOWN},{KNOWN}")
    _names(f"{UNKNOWN},{KNOWN}")
    assert esi.posts == [[UNKNOWN, KNOWN], [UNKNOWN, KNOWN]]
    assert not starmap_mod._alliance_name_misses


@pytest.mark.parametrize("answer", _TRANSIENT)
def test_names_failed_batch_is_not_remembered(esi, answer):
    """A throttle, 5xx, 403 or network failure says nothing about any id:
    no miss is kept, and the next request asks again and resolves."""
    esi.post_answer = lambda ids: answer
    assert _names(str(KNOWN)) == {str(KNOWN): f"Alliance {KNOWN}"}
    assert not starmap_mod._alliance_name_misses

    esi.post_answer = lambda ids: _Resp(200, [{"id": KNOWN, "name": "Sample Alliance",
                                               "category": "alliance"}])
    assert _names(str(KNOWN)) == {str(KNOWN): "Sample Alliance"}
    assert esi.posts == [[KNOWN], [KNOWN]]
    assert starmap_mod._alliance_cache[KNOWN] == "Sample Alliance"


def test_names_remember_a_non_alliance_id_esi_answered(esi):
    """ESI resolves the id, but to a corporation: that is an answer too."""
    esi.post_answer = lambda ids: _Resp(200, [{"id": UNKNOWN, "name": "Sample Corp",
                                               "category": "corporation"}])
    assert _names(str(UNKNOWN)) == {str(UNKNOWN): f"Alliance {UNKNOWN}"}
    _names(str(UNKNOWN))
    assert esi.posts == [[UNKNOWN]]


def test_a_name_resolved_elsewhere_beats_a_remembered_miss(esi):
    """The sov warm-up fills the name cache on its own; a name there wins
    over a miss the route remembered, with no wait for the miss to expire."""
    _names(str(KNOWN))
    assert KNOWN in starmap_mod._alliance_name_misses
    starmap_mod._alliance_cache[KNOWN] = "Sample Alliance"
    assert _names(str(KNOWN)) == {str(KNOWN): "Sample Alliance"}
    assert esi.posts == [[KNOWN]]


def test_names_resolve_known_ids_and_serve_them_from_the_name_cache(esi):
    esi.post_answer = lambda ids: _Resp(200, [{"id": KNOWN, "name": "Sample Alliance",
                                               "category": "alliance"}])
    assert _names(f"{KNOWN},{UNKNOWN}") == {
        str(KNOWN): "Sample Alliance", str(UNKNOWN): f"Alliance {UNKNOWN}"}
    assert _names(f"{KNOWN},{UNKNOWN}") == {
        str(KNOWN): "Sample Alliance", str(UNKNOWN): f"Alliance {UNKNOWN}"}
    assert esi.posts == [[KNOWN, UNKNOWN]]


def test_names_send_each_id_once(esi):
    _names(f"{KNOWN},{UNKNOWN},{KNOWN},{KNOWN},{UNKNOWN}")
    assert esi.posts == [[KNOWN, UNKNOWN]]


def test_names_cap_ids_per_request_at_one_esi_call(esi):
    cap = starmap_mod.MAP_ALLIANCES_MAX_IDS
    assert cap == 1000
    ids = list(range(99000001, 99000001 + cap + 500))
    body = _names(",".join(map(str, ids)))
    assert len(esi.posts) == 1
    assert esi.posts[0] == ids[:cap]
    assert set(body) == {str(i) for i in ids[:cap]}


def test_names_cap_applies_after_duplicates_are_dropped(esi):
    cap = starmap_mod.MAP_ALLIANCES_MAX_IDS
    ids = list(range(99000001, 99000001 + cap))
    _names(",".join(map(str, ids + ids)))
    assert esi.posts == [ids]
