"""app/character_skills.py — pure reads of the synced "skills" cache field
(T-073). No DB, no ESI: `char`/`cache` are simple stand-ins with just the
attributes the two functions actually read.
"""
import json
from types import SimpleNamespace

from app import character_skills
from app.auth import scopes as perms


def _char(scopes=""):
    return SimpleNamespace(scopes=scopes)


def _cache(skills_json=None):
    return SimpleNamespace(skills_json=skills_json)


def test_no_scope_when_the_character_never_shared_the_skills_scope():
    assert character_skills.skill_summary(_char(""), _cache()) == "no_scope"
    assert character_skills.skill_summary(_char("esi-wallet.read_character_wallet.v1"), _cache()) == "no_scope"


def test_none_when_scope_present_but_not_yet_synced():
    assert character_skills.skill_summary(_char(perms.SKILLS), None) is None
    assert character_skills.skill_summary(_char(perms.SKILLS), _cache(None)) is None
    assert character_skills.skill_summary(_char(perms.SKILLS), _cache("")) is None


def test_none_on_corrupt_cached_json():
    assert character_skills.skill_summary(_char(perms.SKILLS), _cache("{not json")) is None
    assert character_skills.skill_summary(_char(perms.SKILLS), _cache("[1, 2]")) is None


def test_parses_a_good_payload():
    payload = {"total_sp": 12_000_000, "unallocated_sp": 500_000, "levels": {"3300": 5}}
    summary = character_skills.skill_summary(_char(perms.SKILLS), _cache(json.dumps(payload)))
    assert summary == payload


def test_scope_check_is_substring_safe_against_other_scopes():
    scopes = f"esi-wallet.read_character_wallet.v1 {perms.SKILLS} esi-clones.read_clones.v1"
    payload = {"total_sp": 1, "unallocated_sp": 0, "levels": {}}
    summary = character_skills.skill_summary(_char(scopes), _cache(json.dumps(payload)))
    assert summary == payload


# ── skill_levels ─────────────────────────────────────────────────────────────

def test_skill_levels_converts_string_keys_to_int():
    summary = {"total_sp": 1, "unallocated_sp": 0, "levels": {"3300": 5, "3301": "4"}}
    assert character_skills.skill_levels(summary) == {3300: 5, 3301: 4}


def test_skill_levels_empty_for_sentinels_and_bad_shapes():
    assert character_skills.skill_levels("no_scope") == {}
    assert character_skills.skill_levels(None) == {}
    assert character_skills.skill_levels({"total_sp": 1, "unallocated_sp": 0}) == {}
    assert character_skills.skill_levels({"levels": "not a dict"}) == {}


def test_skill_levels_skips_unparseable_entries():
    summary = {"levels": {"abc": 5, "3300": "not a number", "3301": 4}}
    assert character_skills.skill_levels(summary) == {3301: 4}
