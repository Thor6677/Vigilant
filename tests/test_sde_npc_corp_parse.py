from app.sde.loader import _parse_npc_corp_item


FIXTURE = {
    "_key": 1000002,  # CBD Corporation
    "allowedMemberRaces": [1],
    "ceoID": 3004049,
    "factionID": 500001,  # Caldari State
    "name": {"en": "CBD Corporation"},
}


def test_parse_npc_corp_item_with_faction():
    assert _parse_npc_corp_item(FIXTURE) == {
        "corporation_id": 1000002, "faction_id": 500001,
    }


def test_parse_npc_corp_item_no_faction():
    # Most NPC corps (mission-agent employers, etc.) have no factionID at
    # all — stored as a row with faction_id: None, not skipped.
    item = {"_key": 1000001, "name": {"en": "Doomheim"}}
    assert _parse_npc_corp_item(item) == {
        "corporation_id": 1000001, "faction_id": None,
    }


def test_parse_npc_corp_item_string_key():
    # `_key` can come through as a string depending on the JSONL row — same
    # coercion idiom as `_parse_invention_item`.
    item = {"_key": "1000002", "factionID": 500001}
    assert _parse_npc_corp_item(item) == {
        "corporation_id": 1000002, "faction_id": 500001,
    }


def test_parse_npc_corp_item_missing_key_returns_none():
    assert _parse_npc_corp_item({"factionID": 500001}) is None
