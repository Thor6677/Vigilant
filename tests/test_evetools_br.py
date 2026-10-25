"""T-082: the best-effort evetools saved-report resolver.

Documents are hand-built in the shape the endpoint returns; ids, hashes and
times are invented. HTTP is stubbed with httpx.MockTransport (nothing leaves
the process)."""
import asyncio
import gzip
import json
import types

import httpx
import pytest

from app.intel import battle_loot as bl
from app.intel import evetools_br as ev

BR = "0123456789abcdef01234567"
T0 = 1_800_000_000
HASH = "a" * 40
SYS_A, SYS_B = 30009001, 30009002


def _doc(**over):
    doc = {
        "status": "success", "new": True, "short": False, "kmsCount": 2,
        "timings": [{"systemID": SYS_A, "start": T0, "end": T0 + 10800},
                    {"systemID": SYS_B, "start": T0 + 3600, "end": T0 + 7200}],
        "relateds": [{"systemID": SYS_A, "start": T0, "end": T0 + 10800,
                      "kms": [{"id": 700001, "hash": HASH, "system": SYS_A, "time": T0 * 1000},
                              {"id": 700002, "hash": "zz", "system": SYS_A, "time": T0 * 1000}]},
                     {"systemID": SYS_B, "kms": []}],
    }
    doc.update(over)
    return doc


def _body(doc_or_bytes, status=200, headers=None):
    """A response whose body is a real stream, like the network gives."""
    data = doc_or_bytes if isinstance(doc_or_bytes, bytes) else json.dumps(doc_or_bytes).encode()
    return httpx.Response(status, headers=headers, stream=httpx.ByteStream(data))


def _run(coro):
    return asyncio.run(coro)


# ── parse_composition (pure) ─────────────────────────────────────────────

def test_new_style_multi_system():
    r = ev.parse_composition(_doc())
    assert isinstance(r, ev.EvetoolsScope)
    assert [(w.system_id, w.start, w.end) for w in r.windows] == [
        (SYS_A, T0, T0 + 10800), (SYS_B, T0 + 3600, T0 + 7200)]
    assert r.kms_count == 2
    assert not hasattr(r, "kill_refs")       # only windows and a count are kept


def test_short_reports_are_counted_the_same_way():
    doc = _doc(short=True)
    del doc["kmsCount"]
    doc["relateds"] = [{"systemID": SYS_A, "kms": [{"id": 700003, "sys": SYS_A, "time": T0}]}]
    assert ev.parse_composition(doc).kms_count == 1


def test_old_style_reports_use_the_related_stamp():
    doc = {"status": "success", "new": False,
           "relateds": [{"relatedKey": f"{SYS_A}/202611071400", "systemID": SYS_A, "time": 1}]}
    r = ev.parse_composition(doc)
    (w,) = r.windows
    assert w.system_id == SYS_A and w.end - w.start == 3 * 3600


def test_processing_and_not_found_have_their_own_messages():
    assert "shortly" in ev.parse_composition({"status": "processing"}).message
    assert "no report" in ev.parse_composition({"status": "error", "error": "Composition not found"}).message


@pytest.mark.parametrize("doc", [
    None, [], "x", {}, {"status": "weird"}, {"status": "success"},
    _doc(timings=[{"systemID": "30009001", "start": T0, "end": T0 + 60}]),     # string id
    _doc(timings=[{"systemID": True, "start": T0, "end": T0 + 60}]),
    _doc(timings=[{"systemID": SYS_A, "start": T0 * 1000, "end": T0 * 1000 + 60}]),   # ms, not s
    _doc(timings=[{"systemID": SYS_A, "start": T0 + 60, "end": T0}]),            # reversed
    _doc(timings=[{"systemID": SYS_A, "start": T0, "end": T0 + 90000}]),         # over 24 h
    _doc(timings=[{"systemID": SYS_A + i, "start": T0, "end": T0 + 60} for i in range(11)]),
    _doc(timings=["x"]),
])
def test_unusable_documents_are_rejected(doc):
    assert isinstance(ev.parse_composition(doc), ev.EvetoolsError)


def test_non_int_kill_ids_are_not_counted():
    doc = _doc()
    del doc["kmsCount"]
    doc["relateds"][0]["kms"] = [{"id": "700001"}, {"id": True}, {"id": -4}, {"id": 700009}, "x", 5]
    assert ev.parse_composition(doc).kms_count == 1


@pytest.mark.parametrize("junk", [5, True, "x", {"a": 1}, [5], [None], [[1]], [{"kms": 5}], [{"kms": True}],
                                  [{"kms": [5, None, True]}], [{"kms": {"id": 1}}]])
def test_odd_shapes_never_raise(junk):
    for key in ("relateds", "kms", "timings"):
        doc = _doc(**{key: junk})
        assert isinstance(ev.parse_composition(doc), (ev.EvetoolsScope, ev.EvetoolsError))
    doc = _doc(kmsCount=junk)
    assert isinstance(ev.parse_composition(doc), (ev.EvetoolsScope, ev.EvetoolsError))


def test_deeply_nested_documents_never_raise():
    deep = []
    for _ in range(5000):
        deep = [deep]
    assert isinstance(ev.parse_composition(_doc(relateds=deep, kmsCount=None)), ev.EvetoolsError | ev.EvetoolsScope)
    assert isinstance(ev.parse_composition({"status": "success", "timings": deep}), ev.EvetoolsError)


# ── resolve_br (stubbed HTTP) ────────────────────────────────────────────

@pytest.fixture
def http(monkeypatch):
    """Route AsyncClient through a MockTransport; record every request."""
    state = types.SimpleNamespace(requests=[], kwargs=None, handler=None)

    def handler(request):
        state.requests.append(request)
        return state.handler(request)

    real = httpx.AsyncClient

    def factory(*a, **kw):
        state.kwargs = kw
        return real(transport=httpx.MockTransport(handler), **kw)

    state.handler = lambda r: _body(_doc())
    monkeypatch.setattr(ev.httpx, "AsyncClient", factory)
    monkeypatch.setattr(ev, "get_settings", lambda: types.SimpleNamespace(battle_loot_evetools_resolve=True))
    ev._cache.clear()
    ev._user_hits.clear()
    ev._process_hits.clear()
    return state


async def _all_exist(ids):
    return set(ids)


def test_the_request_is_the_constant_url_plus_the_id(http):
    r = _run(ev.resolve_br(BR, 1, _all_exist))
    assert isinstance(r, ev.EvetoolsScope)
    (req,) = http.requests
    assert str(req.url) == f"https://br.evetools.org/newapi/br/composition/{BR}"
    assert req.headers["user-agent"].startswith("Vigilant/")
    assert "cookie" not in req.headers
    assert http.kwargs["follow_redirects"] is False and http.kwargs["trust_env"] is False
    assert http.kwargs["timeout"] == ev.FETCH_TIMEOUT_SECONDS


def test_a_redirect_is_a_failure_not_followed(http):
    http.handler = lambda r: _body(b"", 302, {"location": "https://example.invalid/x"})
    r = _run(ev.resolve_br(BR, 1, _all_exist))
    assert isinstance(r, ev.EvetoolsError) and r.message == bl.SORRY_PASTE_RELATED
    assert len(http.requests) == 1


def test_body_cap_rejects_an_otherwise_valid_report(http, monkeypatch):
    """The padded document parses fine; only the cap can make this fail."""
    padded = json.dumps({**_doc(), "pad": "x" * 5000}).encode()
    http.handler = lambda r: _body(padded)
    assert isinstance(_run(ev.resolve_br("1" * 24, 1, _all_exist)), ev.EvetoolsScope)
    ev._cache.clear()
    monkeypatch.setattr(ev, "MAX_BODY_BYTES", 1000)
    assert isinstance(_run(ev.resolve_br("2" * 24, 1, _all_exist)), ev.EvetoolsError)


def test_a_small_gzip_body_cannot_inflate_past_the_cap(http, monkeypatch):
    padded = json.dumps({**_doc(), "pad": "x" * 200_000}).encode()
    squeezed = gzip.compress(padded)
    assert len(squeezed) < 2000
    http.handler = lambda r: _body(squeezed, headers={"content-encoding": "gzip"})
    assert isinstance(_run(ev.resolve_br("1" * 24, 1, _all_exist)), ev.EvetoolsScope)
    ev._cache.clear()
    monkeypatch.setattr(ev, "MAX_BODY_BYTES", 50_000)
    seen = []
    real = ev.zlib.decompressobj

    def spy(*a, **k):
        d = real(*a, **k)

        class Spy:
            unconsumed_tail = property(lambda self: d.unconsumed_tail)

            def decompress(self, data, max_length=0):
                out = d.decompress(data, max_length)
                seen.append(len(out))
                return out

            def flush(self, n):
                return d.flush(n)
        return Spy()
    monkeypatch.setattr(ev.zlib, "decompressobj", spy)
    assert isinstance(_run(ev.resolve_br("2" * 24, 1, _all_exist)), ev.EvetoolsError)
    assert max(seen) <= 50_001                    # inflation is bounded per call, not after the fact


def test_unknown_content_encodings_are_refused(http):
    http.handler = lambda r: _body(b"{}", headers={"content-encoding": "br"})
    assert isinstance(_run(ev.resolve_br(BR, 1, _all_exist)), ev.EvetoolsError)


def test_a_slow_trickle_hits_the_total_deadline(http, monkeypatch):
    monkeypatch.setattr(ev, "TOTAL_DEADLINE_SECONDS", 0.2)

    async def slow(request):
        await asyncio.sleep(5)
        return _body(_doc())
    http.handler = slow
    r = _run(ev.resolve_br(BR, 1, _all_exist))
    assert isinstance(r, ev.EvetoolsError) and r.message == bl.SORRY_PASTE_RELATED


def test_at_most_two_fetches_run_at_once(http):
    state = {"now": 0, "peak": 0}

    async def handler(request):
        state["now"] += 1
        state["peak"] = max(state["peak"], state["now"])
        await asyncio.sleep(0.05)
        state["now"] -= 1
        return _body(_doc())
    http.handler = handler

    async def go():
        return await asyncio.gather(*(ev.resolve_br(f"{i:024x}", 500 + i, _all_exist) for i in range(6)))
    results = _run(go())
    assert all(isinstance(r, ev.EvetoolsScope) for r in results)
    assert state["peak"] == ev.MAX_CONCURRENT_FETCHES == 2


def test_resolve_never_raises_whatever_happens(http):
    deep = b"[" * 100_000 + b"]" * 100_000
    http.handler = lambda r: _body(deep)
    assert isinstance(_run(ev.resolve_br(BR, 1, _all_exist)), ev.EvetoolsError)

    async def boom(ids):
        raise RuntimeError("db gone")
    http.handler = lambda r: _body(_doc())
    assert isinstance(_run(ev.resolve_br("3" * 24, 1, boom)), ev.EvetoolsError)


def test_expired_cache_entries_are_purged_on_write(http, monkeypatch):
    _run(ev.resolve_br("4" * 24, 1, _all_exist))
    real = ev.time.monotonic
    monkeypatch.setattr(ev.time, "monotonic", lambda: real() + ev.CACHE_TTL_SECONDS + 5)
    _run(ev.resolve_br("5" * 24, 2, _all_exist))
    assert list(ev._cache) == ["5" * 24]


# ── /battle/<n> ───────────────────────────────────────────────────────────

def _battle(**over):
    doc = {"battle": {"solar_system": SYS_A, "battle_start": T0, "battle_end": T0 + 7200},
           "kms": [{"id": 700001, "hash": HASH, "system": SYS_A, "time": T0 * 1000},
                   {"id": 700002, "hash": HASH, "system": SYS_A, "time": T0 * 1000}]}
    doc["battle"].update(over)
    return doc


def test_parse_battle_gives_one_window_and_a_count():
    r = ev.parse_battle(_battle())
    assert [(w.system_id, w.start, w.end) for w in r.windows] == [(SYS_A, T0, T0 + 7200)]
    assert r.kms_count == 2


def test_parse_battle_accepts_milliseconds():
    r = ev.parse_battle(_battle(battle_start=T0 * 1000, battle_end=(T0 + 7200) * 1000))
    assert (r.windows[0].start, r.windows[0].end) == (T0, T0 + 7200)


@pytest.mark.parametrize("doc", [
    None, [], {}, {"battle": 5}, {"battle": {}}, _battle(solar_system="30009001"),
    _battle(solar_system=2 ** 40), _battle(battle_start="2026-01-01"), _battle(battle_end=T0 - 5),
    _battle(battle_end=T0 + 90000), _battle(battle_start=True), _battle(battle_end=99_999_999_999_999),
])
def test_parse_battle_rejects_unusable_documents(doc):
    assert isinstance(ev.parse_battle(doc), ev.EvetoolsError)


def test_battle_request_is_the_constant_url_plus_the_int(http):
    http.handler = lambda r: _body(_battle())
    r = _run(ev.resolve_battle(4242, 1, _all_exist))
    assert isinstance(r, ev.EvetoolsScope)
    (req,) = http.requests
    assert str(req.url) == "https://br.evetools.org/newapi/br/battle/4242"
    assert http.kwargs["follow_redirects"] is False and http.kwargs["trust_env"] is False


def test_battle_results_are_cached_rate_limited_and_kill_switched(http, monkeypatch):
    http.handler = lambda r: _body(_battle())
    _run(ev.resolve_battle(4242, 1, _all_exist))
    _run(ev.resolve_battle(4242, 2, _all_exist))
    assert len(http.requests) == 1 and ev.cached_resolution("battle:4242") is not None
    for n in range(10, 10 + ev.USER_LIMIT):
        _run(ev.resolve_battle(n, 9, _all_exist))
    assert "Try again in" in _run(ev.resolve_battle(99, 9, _all_exist)).message
    monkeypatch.setattr(ev, "get_settings", lambda: types.SimpleNamespace(battle_loot_evetools_resolve=False))
    before = len(http.requests)
    assert _run(ev.resolve_battle(12345, 3, _all_exist)).message == bl.SORRY_PASTE_RELATED
    assert len(http.requests) == before


def test_battle_failures_use_the_fallback_message(http):
    http.handler = lambda r: _body(b"", 503)
    assert _run(ev.resolve_battle(7, 1, _all_exist)).message == bl.SORRY_PASTE_RELATED
    http.handler = lambda r: _body({"battle": None})
    assert _run(ev.resolve_battle(8, 1, _all_exist)).message == bl.SORRY_PASTE_RELATED


def test_src_cache_keys():
    assert ev.src_cache_key(f"evetools:{BR}") == BR
    assert ev.src_cache_key("evetools-battle:4242") == "battle:4242"


def test_non_json_and_http_errors_give_the_fallback_message(http):
    http.handler = lambda r: _body(b"<html>challenge</html>")
    assert _run(ev.resolve_br(BR, 1, _all_exist)).message == bl.SORRY_PASTE_RELATED
    http.handler = lambda r: _body(b"", 503)
    assert _run(ev.resolve_br("1" * 24, 1, _all_exist)).message == bl.SORRY_PASTE_RELATED


def test_a_network_error_never_raises(http):
    def boom(request):
        raise httpx.ConnectError("down")
    http.handler = boom
    assert isinstance(_run(ev.resolve_br(BR, 1, _all_exist)), ev.EvetoolsError)


def test_unknown_systems_are_refused(http):
    async def only_a(ids):
        return {SYS_A}
    assert isinstance(_run(ev.resolve_br(BR, 1, only_a)), ev.EvetoolsError)


def test_a_cache_hit_makes_no_second_call(http):
    _run(ev.resolve_br(BR, 1, _all_exist))
    r = _run(ev.resolve_br(BR, 2, _all_exist))
    assert isinstance(r, ev.EvetoolsScope) and len(http.requests) == 1
    assert ev.cached_resolution(BR) is r


def test_failures_are_not_cached(http):
    http.handler = lambda r: _body(b"", 503)
    _run(ev.resolve_br(BR, 1, _all_exist))
    http.handler = lambda r: _body(_doc())
    assert isinstance(_run(ev.resolve_br(BR, 1, _all_exist)), ev.EvetoolsScope)


def test_per_user_rate_limit(http):
    for i in range(ev.USER_LIMIT):
        assert isinstance(_run(ev.resolve_br(f"{i:024x}", 7, _all_exist)), ev.EvetoolsScope)
    r = _run(ev.resolve_br("f" * 24, 7, _all_exist))
    assert isinstance(r, ev.EvetoolsError) and "Try again in" in r.message
    assert len(http.requests) == ev.USER_LIMIT
    # another user is unaffected
    assert isinstance(_run(ev.resolve_br("e" * 24, 8, _all_exist)), ev.EvetoolsScope)


def test_per_process_rate_limit(http, monkeypatch):
    monkeypatch.setattr(ev, "PROCESS_LIMIT", 3)
    for i in range(3):
        _run(ev.resolve_br(f"{i:024x}", 100 + i, _all_exist))
    r = _run(ev.resolve_br("f" * 24, 200, _all_exist))
    assert isinstance(r, ev.EvetoolsError) and "Try again in" in r.message


def test_kill_switch_makes_no_call(http, monkeypatch):
    monkeypatch.setattr(ev, "get_settings", lambda: types.SimpleNamespace(battle_loot_evetools_resolve=False))
    r = _run(ev.resolve_br(BR, 1, _all_exist))
    assert r.message == bl.SORRY_PASTE_RELATED and http.requests == []


def test_the_setting_defaults_on():
    from app.config import Settings
    assert Settings.model_fields["battle_loot_evetools_resolve"].default is True
