"""Entity names: only ESI's own answer about an id is remembered as "no name".

`resolve_entity_names` (app/intel/recent_battles.py) feeds corp and alliance
names to the battle, kill, loot, wormhole and entity pages. It used to store
every id of any batch ESI did not answer with a 200 as "no name", and kept
that for the life of the process. A passing 502 or a throttle therefore
blanked those names until a restart. ESI also refuses a whole
/universe/names/ batch (404) when one id in it is invalid, so one bad id
blanked up to 999 good ones the same way.

Now a miss is recorded only for an id ESI answered about by itself (left out
of a 200, or a 404 for a batch of that id alone), and it expires after
ENTITY_NAME_MISS_TTL. A refused multi-id batch is split in halves to find the
bad id, within ENTITY_NAME_SPLIT_CALLS extra calls per resolve.

ESI is a stub httpx client that records each call; the clock is the module's
`time`. Ids and names are invented.
"""
import asyncio
import types

import httpx
import pytest

from app.intel import recent_battles as rb

_NS = types.SimpleNamespace

CORP_A, CORP_B, ALLI_C = 98100001, 98100002, 99100003
BAD = 1                     # an id ESI cannot name
NAMES = {CORP_A: "Sample Corp A", CORP_B: "Sample Corp B", ALLI_C: "Sample Alliance C"}


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
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def _esi_like(names):
    """What ESI does: a 404 for the whole batch if any id in it is unknown,
    otherwise a 200 naming every id."""
    def answer(ids):
        if any(i not in names for i in ids):
            return _Resp(404, {"error": "Ensure all IDs are valid before resolving."})
        return _Resp(200, [{"id": i, "name": names[i], "category": "corporation"}
                           for i in ids])
    return answer


@pytest.fixture
def esi(monkeypatch):
    """A recording stand-in for ESI, a settable clock, and empty caches.

    `esi.answer(ids)` decides each response; returning an Exception instance
    raises it."""
    state = _NS(posts=[], clock=[1000.0], answer=_esi_like(NAMES))

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, headers=None):
            state.posts.append(list(json))
            answer = state.answer(list(json))
            if isinstance(answer, Exception):
                raise answer
            return answer

    monkeypatch.setattr(rb, "httpx", _NS(AsyncClient=_Client))
    monkeypatch.setattr(rb, "time", _NS(monotonic=lambda: state.clock[0]))
    monkeypatch.setattr(rb, "user_agent", lambda: "test-agent")
    monkeypatch.setattr(rb, "_entity_name_cache", {})
    monkeypatch.setattr(rb, "_entity_name_misses", {})
    return state


def _resolve(ids):
    return _run(rb.resolve_entity_names(ids))


# ── Resolved names ────────────────────────────────────────────────────

def test_resolved_names_are_kept_and_not_asked_again(esi):
    assert _resolve([CORP_A, CORP_B]) == {CORP_A: NAMES[CORP_A], CORP_B: NAMES[CORP_B]}
    esi.clock[0] += 10 * rb.ENTITY_NAME_MISS_TTL
    assert _resolve([CORP_A, CORP_B]) == {CORP_A: NAMES[CORP_A], CORP_B: NAMES[CORP_B]}
    assert esi.posts == [[CORP_A, CORP_B]]


def test_each_id_is_asked_for_once(esi):
    """A caller may repeat an id (the wormhole page lists an alliance once
    per resident corp); the batch sends it once."""
    assert _resolve([CORP_A, ALLI_C, CORP_B, ALLI_C, 0, None]) == {
        CORP_A: NAMES[CORP_A], ALLI_C: NAMES[ALLI_C], CORP_B: NAMES[CORP_B]}
    assert esi.posts == [[CORP_A, ALLI_C, CORP_B]]


# ── Failures that say nothing about any id ────────────────────────────

_TRANSIENT = [_Resp(500), _Resp(502), _Resp(503), _Resp(504), _Resp(420), _Resp(429),
              _Resp(403), _Resp(400), _Resp(200, ValueError("not json")),
              httpx.ReadTimeout("timed out"), RuntimeError("connection reset")]


@pytest.mark.parametrize("answer", _TRANSIENT)
def test_a_failed_batch_caches_nothing_and_is_asked_again(esi, answer):
    esi.answer = lambda ids: answer
    assert _resolve([CORP_A, CORP_B]) == {}
    assert rb._entity_name_misses == {}
    assert rb._entity_name_cache == {}

    assert _resolve([CORP_A, CORP_B]) == {}
    assert len(esi.posts) == 2

    esi.answer = _esi_like(NAMES)
    assert _resolve([CORP_A, CORP_B]) == {CORP_A: NAMES[CORP_A], CORP_B: NAMES[CORP_B]}
    assert len(esi.posts) == 3


@pytest.mark.parametrize("status", [400, 403, 420, 422, 429, 500, 502, 503])
def test_only_a_404_marks_a_single_id_as_nameless(esi, status):
    esi.answer = lambda ids: _Resp(status)
    _resolve([CORP_A])
    assert rb._entity_name_misses == {}
    _resolve([CORP_A])
    assert esi.posts == [[CORP_A], [CORP_A]]


# ── Refused multi-id batches ──────────────────────────────────────────

def test_a_refused_multi_id_batch_caches_nothing_negative(esi, monkeypatch):
    """With no budget to split it, a 404 for several ids leaves every one of
    them to be asked again: ESI may have refused it for any single one."""
    monkeypatch.setattr(rb, "ENTITY_NAME_SPLIT_CALLS", 0)
    assert _resolve([CORP_A, CORP_B, BAD]) == {}
    assert rb._entity_name_misses == {}
    assert rb._entity_name_cache == {}

    assert _resolve([CORP_A, CORP_B]) == {CORP_A: NAMES[CORP_A], CORP_B: NAMES[CORP_B]}
    assert esi.posts == [[CORP_A, CORP_B, BAD], [CORP_A, CORP_B]]


def test_one_bad_id_is_isolated_and_the_rest_are_named(esi):
    assert _resolve([CORP_A, CORP_B, ALLI_C, BAD]) == NAMES
    assert set(rb._entity_name_misses) == {BAD}
    assert len(esi.posts) <= 1 + rb.ENTITY_NAME_SPLIT_CALLS
    assert [BAD] in esi.posts

    # Everything is answered from memory until the miss expires
    sent = len(esi.posts)
    assert _resolve([CORP_A, CORP_B, ALLI_C, BAD]) == NAMES
    assert len(esi.posts) == sent

    esi.clock[0] += rb.ENTITY_NAME_MISS_TTL + 1
    assert _resolve([CORP_A, CORP_B, ALLI_C, BAD]) == NAMES
    assert esi.posts[sent:] == [[BAD]]


def test_a_failed_half_caches_nothing_negative(esi):
    """The halves of a refused batch are asked like any batch: a 5xx for
    one says nothing about its ids."""
    def answer(ids):
        if len(ids) == 4:
            return _Resp(404)
        if BAD in ids:
            return _Resp(503)
        return _esi_like(NAMES)(ids)
    esi.answer = answer
    assert _resolve([CORP_A, CORP_B, ALLI_C, BAD]) == {CORP_A: NAMES[CORP_A],
                                                       CORP_B: NAMES[CORP_B]}
    assert rb._entity_name_misses == {}
    assert esi.posts == [[CORP_A, CORP_B, ALLI_C, BAD], [CORP_A, CORP_B], [ALLI_C, BAD]]


def test_splitting_stays_within_its_budget_and_converges(esi):
    """One bad id among many costs at most 1 + ENTITY_NAME_SPLIT_CALLS calls
    per resolve; the good names found are kept, so later resolves close in on
    the bad id instead of starting over."""
    names = {98200000 + n: f"Corp {n}" for n in range(300)}
    esi.answer = _esi_like(names)
    ids = list(names) + [BAD]

    calls = []
    for _ in range(6):
        before = len(esi.posts)
        got = _resolve(ids)
        calls.append(len(esi.posts) - before)
        if BAD in rb._entity_name_misses:
            break
    assert got == names
    assert set(rb._entity_name_misses) == {BAD}
    assert all(n <= 1 + rb.ENTITY_NAME_SPLIT_CALLS for n in calls)
    assert len(calls) <= 3


# ── Ids a 200 left out ────────────────────────────────────────────────

def test_an_id_left_out_of_a_200_is_remembered_then_asked_again(esi):
    esi.answer = lambda ids: _Resp(200, [{"id": CORP_A, "name": NAMES[CORP_A]}])
    assert _resolve([CORP_A, CORP_B]) == {CORP_A: NAMES[CORP_A]}
    assert set(rb._entity_name_misses) == {CORP_B}
    assert CORP_B not in rb._entity_name_cache

    esi.clock[0] += rb.ENTITY_NAME_MISS_TTL - 1
    assert _resolve([CORP_A, CORP_B]) == {CORP_A: NAMES[CORP_A]}
    assert len(esi.posts) == 1

    esi.clock[0] += 2
    esi.answer = _esi_like(NAMES)
    assert _resolve([CORP_A, CORP_B]) == {CORP_A: NAMES[CORP_A], CORP_B: NAMES[CORP_B]}
    assert esi.posts[1:] == [[CORP_B]]
    assert rb._entity_name_misses == {}


def test_a_single_id_404_is_remembered_then_asked_again(esi):
    assert _resolve([BAD]) == {}
    assert _resolve([BAD]) == {}
    assert esi.posts == [[BAD]]

    esi.clock[0] += rb.ENTITY_NAME_MISS_TTL + 1
    assert _resolve([BAD]) == {}
    assert esi.posts == [[BAD], [BAD]]
