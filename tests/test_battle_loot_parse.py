"""T-082: battle link parsing. Pure; systems, ids and stamps are invented."""
import json
from datetime import datetime, timezone
from urllib.parse import quote

import pytest

from app.intel import battle_loot as bl

SYS = 30009001
STAMP = "202611071400"
EPOCH = int(datetime(2026, 11, 7, 14, 0, tzinfo=timezone.utc).timestamp())
BR_ID = "0123456789abcdef01234567"


def _opts(doc) -> str:
    return quote(json.dumps(doc, separators=(",", ":")), safe="")


@pytest.mark.parametrize("host", ["zkillboard.com", "www.zkillboard.com", "killmail.app",
                                  "br.evetools.org", "kb.evetools.org"])
@pytest.mark.parametrize("scheme", ["http", "https"])
@pytest.mark.parametrize("tail", ["", "/"])
def test_related_links_on_every_host(host, scheme, tail):
    ref = bl.parse_br_link(f"{scheme}://{host}/related/{SYS}/{STAMP}{tail}")
    assert ref.kind == "related"
    assert ref.system_ids == (SYS,) and ref.stamp == EPOCH
    assert (ref.hours_before, ref.hours_after) == (1, 2)


def test_minutes_are_floored_to_the_hour():
    assert bl.parse_br_link(f"https://zkillboard.com/related/{SYS}/202611071437/").stamp == EPOCH


def test_query_string_and_fragment_are_ignored():
    ref = bl.parse_br_link(f"https://zkillboard.com/related/{SYS}/{STAMP}/?x=1#top")
    assert ref.stamp == EPOCH


def test_zkill_options_suffix():
    opts = _opts({"systems": [SYS, 30009002, 30009003], "hoursAfter": 4, "A": [93000001]})
    ref = bl.parse_br_link(f"https://zkillboard.com/related/{SYS}/{STAMP}/o/{opts}/")
    assert ref.system_ids == (SYS, 30009002, 30009003)
    assert (ref.hours_before, ref.hours_after) == (1, 4)
    scope = bl.scope_from_related(ref.system_ids, ref.stamp, ref.hours_before, ref.hours_after)
    assert len(scope) == 3
    assert {(w.start, w.end) for w in scope} == {(EPOCH - 3600, EPOCH + 4 * 3600)}


def test_a_listed_set_replaces_the_path_system():
    """zKill reads `systems` as the selection itself."""
    opts = _opts({"systems": [30009002, 30009003, 30009002]})
    ref = bl.parse_br_link(f"https://www.zkillboard.com/related/{SYS}/{STAMP}/o/{opts}/")
    assert ref.system_ids == (30009002, 30009003)


def test_an_empty_or_missing_set_keeps_the_path_system():
    for doc in ({"systems": []}, {"hoursAfter": 3}):
        ref = bl.parse_br_link(f"https://zkillboard.com/related/{SYS}/{STAMP}/o/{_opts(doc)}/")
        assert ref.system_ids == (SYS,)


def test_more_than_ten_systems_are_cut_not_refused():
    opts = _opts({"systems": list(range(1, 15))})
    ref = bl.parse_br_link(f"https://zkillboard.com/related/{SYS}/{STAMP}/o/{opts}/")
    assert ref.system_ids == tuple(range(1, 11))


def test_hours_accept_digit_strings_and_stay_bounded():
    opts = _opts({"hoursBefore": "3", "hoursAfter": "99"})
    ref = bl.parse_br_link(f"https://zkillboard.com/related/{SYS}/{STAMP}/o/{opts}/")
    assert (ref.hours_before, ref.hours_after) == (3, 12)


def test_default_window_is_one_hour_before_two_after():
    (w,) = bl.scope_from_related([SYS], EPOCH)
    assert (w.start, w.end) == (EPOCH - 3600, EPOCH + 7200)


def test_hours_are_clamped():
    (w,) = bl.scope_from_related([SYS], EPOCH, 0, 99)
    assert (w.start, w.end) == (EPOCH - 3600, EPOCH + 12 * 3600)
    opts = _opts({"hoursBefore": 0, "hoursAfter": 99})
    ref = bl.parse_br_link(f"https://zkillboard.com/related/{SYS}/{STAMP}/o/{opts}/")
    assert (ref.hours_before, ref.hours_after) == (1, 12)


@pytest.mark.parametrize("tail", ["", "/"])
def test_evetools_saved_report(tail):
    ref = bl.parse_br_link(f"https://br.evetools.org/br/{BR_ID}{tail}")
    assert ref.kind == "evetools_br" and ref.br_id == BR_ID


@pytest.mark.parametrize("text", [
    "https://zkillboard.com.evil.example/related/30009001/202611071400/",   # lookalike
    "https://evilzkillboard.com/related/30009001/202611071400/",
    "https://zkillboard.com.attacker/related/30009001/202611071400/",
    "https://sub.zkillboard.com/related/30009001/202611071400/",            # no suffix match
    "https://zkillboard.com./related/30009001/202611071400/",               # trailing dot
    "https://127.0.0.1/related/30009001/202611071400/",                     # IP literal
    "https://[::1]/related/30009001/202611071400/",
    "https://" + "user" + "@zkillboard.com/related/30009001/202611071400/", # userinfo
    "https://zkillboard.com@evil.example/related/30009001/202611071400/",
    "https://zkillboard.com:8443/related/30009001/202611071400/",           # port
    "https://zkillboard.com:443/related/30009001/202611071400/",
    "https://zkіllboard.com/related/30009001/202611071400/",                # homograph
    "ftp://zkillboard.com/related/30009001/202611071400/",
    "zkillboard.com/related/30009001/202611071400/",                        # no scheme
    "https://zkillboard.com/related/30009001/\n202611071400/",              # control (inside)
    "https://zkillboard.com/related/30009001/2026110714\x0000/",
    "https://zkillboard.com/related/30009001/202611071400/ extra",
    "https://zkillboard.com/related/30009001/20261107140/",                 # short stamp
    "https://zkillboard.com/related/abc/202611071400/",
    "https://zkillboard.com/related/30009001/202613071400/",                # month 13
    "https://zkillboard.com/related/30009001/200211071400/",                # before 2003
    "https://zkillboard.com/",
    "https://zkillboard.com/character/93000001/",
    "",
    "   ",
])
def test_rejects(text):
    with pytest.raises(bl.BrError):
        bl.parse_br_link(text)


def test_too_long():
    with pytest.raises(bl.BrError):
        bl.parse_br_link("https://zkillboard.com/related/30009001/202611071400/?" + "a" * 2000)


@pytest.mark.parametrize("opts", [
    "not-json", quote("[1,2]"), quote('"str"'), quote("42"),
    _opts({"systems": [2 ** 40]}), _opts({"systems": [0]}),             # not an EVE system id
    _opts({"systems": [-5]}), _opts({"systems": ["a"]}), _opts({"systems": [True]}),
    _opts({"systems": "30009002"}),
    _opts({"hoursBefore": "2h"}), _opts({"hoursAfter": 1.5}), _opts({"hoursBefore": "-1"}),
])
def test_rejects_bad_options(opts):
    with pytest.raises(bl.BrError):
        bl.parse_br_link(f"https://zkillboard.com/related/{SYS}/{STAMP}/o/{opts}/")


def test_options_only_on_zkill_hosts():
    opts = _opts({"hoursAfter": 4})
    for host in ("killmail.app", "br.evetools.org", "kb.evetools.org"):
        with pytest.raises(bl.BrError):
            bl.parse_br_link(f"https://{host}/related/{SYS}/{STAMP}/o/{opts}/")


@pytest.mark.parametrize("text", [
    "https://zkillboard.com/br/12345/",
    "https://killmail.app/br/abc123",
    "https://zkillboard.com/kill/123456/",
    "https://killmail.app/kill/123456/",
    f"https://br.evetools.org/br/{BR_ID.upper()}",         # uppercase hex
    f"https://br.evetools.org/br/{BR_ID[:-1]}",            # short id
    f"https://br.evetools.org/br/{BR_ID}0",                # long id
    f"https://zkillboard.com/br/{BR_ID}",                  # evetools id shape on the wrong host
])
def test_known_unsupported_paths_get_a_reason(text):
    with pytest.raises(bl.BrError) as e:
        bl.parse_br_link(text)
    assert str(e.value)


def test_messages_never_echo_the_pasted_text():
    with pytest.raises(bl.BrError) as e:
        bl.parse_br_link("https://evil.example/<script>alert(1)</script>")
    assert "evil" not in str(e.value) and "script" not in str(e.value)


def test_windows_round_trip():
    scope = bl.scope_from_related([SYS, 30009002], EPOCH)
    assert bl.parse_windows(bl.window_params(scope)) == scope


@pytest.mark.parametrize("raw", [
    ["x"], ["30009001:1:2:3"], ["30009001:abc:def"], [f"30009001:{EPOCH}:{EPOCH}"],
    [f"30009001:{EPOCH}:{EPOCH - 5}"], [f"30009001:{EPOCH}:{EPOCH + 86401}"],
    [f"{30009001 + i}:{EPOCH}:{EPOCH + 60}" for i in range(11)], [],
])
def test_parse_windows_rejects(raw):
    with pytest.raises(bl.BrError):
        bl.parse_windows(raw)


def test_windows_dedupe_and_sort():
    a, b = f"30009002:{EPOCH}:{EPOCH + 60}", f"30009001:{EPOCH - 100}:{EPOCH + 60}"
    scope = bl.parse_windows([a, b, a])
    assert [w.system_id for w in scope] == [30009001, 30009002]


def test_in_scope_is_inclusive_at_both_ends():
    scope = bl.parse_windows([f"{SYS}:{EPOCH}:{EPOCH + 3600}"])
    at = lambda s: datetime.fromtimestamp(s, timezone.utc).replace(tzinfo=None)
    assert bl.in_scope(scope, SYS, at(EPOCH)) and bl.in_scope(scope, SYS, at(EPOCH + 3600))
    assert not bl.in_scope(scope, SYS, at(EPOCH - 1)) and not bl.in_scope(scope, SYS, at(EPOCH + 3601))
    assert not bl.in_scope(scope, SYS + 1, at(EPOCH))


def test_evetools_battle_links_are_resolved_not_refused():
    for tail in ("", "/"):
        ref = bl.parse_br_link(f"https://br.evetools.org/battle/4242{tail}")
        assert ref.kind == "evetools_battle" and ref.battle_id == 4242


@pytest.mark.parametrize("text", [
    "https://br.evetools.org/battle/abc", "https://br.evetools.org/battle/",
    "https://br.evetools.org/battle/" + "9" * 11, "https://zkillboard.com/battle/12",
    "https://killmail.app/battle/12",
])
def test_bad_battle_links_are_rejected(text):
    with pytest.raises(bl.BrError):
        bl.parse_br_link(text)


@pytest.mark.parametrize("text", [
    f"https://zkillboard.com/related/{2 ** 40}/{STAMP}/",              # system id past any EVE id
    "https://zkillboard.com/related/9999999999/202601011400/",
    "https://zkillboard.com/related/30009001/999912311400/",           # year 9999
    "https://zkillboard.com/related/30009001/210101011400/",           # past 2100
])
def test_crafted_ids_and_stamps_are_rejected_cleanly(text):
    with pytest.raises(bl.BrError):
        bl.parse_br_link(text)


def test_scope_bounds():
    with pytest.raises(bl.BrError):
        bl.parse_windows([f"{2 ** 33}:{EPOCH}:{EPOCH + 60}"])
    with pytest.raises(bl.BrError):
        bl.parse_windows([f"{SYS}:99999999999:99999999990"])
    with pytest.raises(bl.BrError):
        bl.parse_windows([f"{SYS}:{EPOCH}:99999999999"])
