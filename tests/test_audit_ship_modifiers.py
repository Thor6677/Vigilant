"""scripts/audit_ship_modifiers.py finds the ISS-015 bug class and nothing else.

Seeds a temp SDE with three hulls: one whose modifier filter matches its
traits' weapon family, one with the historical Sacrilege shape (missile
traits, a turret-skill filter), and an industrial carrying the hidden
drone-damage effect that is known-correct.
"""
import importlib.util
import os
import sqlite3
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load():
    spec = importlib.util.spec_from_file_location(
        "audit_ship_modifiers", os.path.join(ROOT, "scripts/audit_ship_modifiers.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _seed() -> sqlite3.Connection:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    c = sqlite3.connect(tmp.name)
    c.executescript("""
        CREATE TABLE sde_types (type_id INTEGER, type_name TEXT, group_id INTEGER, category_id INTEGER, published INTEGER);
        CREATE TABLE sde_type_bonuses (id INTEGER PRIMARY KEY, type_id INTEGER, bonus_value REAL, is_role_bonus INTEGER, scaling_skill_id INTEGER, target_type_id INTEGER, bonus_keyword TEXT);
        CREATE TABLE sde_effects (effect_id INTEGER, effect_name TEXT, effect_category INTEGER);
        CREATE TABLE sde_type_effects (id INTEGER PRIMARY KEY, type_id INTEGER, effect_id INTEGER, is_default INTEGER);
        CREATE TABLE sde_modifiers (id INTEGER PRIMARY KEY, effect_id INTEGER, func TEXT, domain TEXT, modified_attribute_id INTEGER, modifying_attribute_id INTEGER, operator INTEGER, filter_type TEXT, filter_value INTEGER);
    """)
    # skills: group 256 = Missiles, 255 = Gunnery, 273 = Drones, 1210 = Armor
    c.executemany("INSERT INTO sde_types VALUES (?,?,?,?,?)", [
        (3324, "Heavy Missiles", 256, 16, 1),
        (3306, "Medium Energy Turret", 255, 16, 1),
        (3436, "Drones", 273, 16, 1),
        (3335, "Amarr Cruiser", 257, 16, 1),
        (3394, "Hull Upgrades", 1210, 16, 1),
        (100, "Good Missile Boat", 26, 6, 1),
        (101, "Broken Missile Boat", 26, 6, 1),
        (102, "Some Hauler", 28, 6, 1),
        (103, "Unpublished Thing", 26, 6, 0),
    ])
    c.executemany("INSERT INTO sde_type_bonuses (type_id,bonus_value,is_role_bonus,scaling_skill_id,target_type_id,bonus_keyword) VALUES (?,?,?,?,?,?)", [
        (100, 5.0, 0, 3335, 3324, "bonus to heavy missile damage"),
        (101, 5.0, 0, 3335, 3324, "bonus to heavy missile damage"),
        (102, 5.0, 0, None, None, "bonus to cargo capacity"),
        (103, 5.0, 0, 3335, 3324, "bonus to heavy missile damage"),
    ])
    c.executemany("INSERT INTO sde_effects VALUES (?,?,?)", [
        (900, "shipMissileDamageAC", 0),
        (901, "shipTurretDamageACwrong", 0),
        (902, "industrialBonusDroneDamage", 0),
        (903, "shipArmorHPBonus", 0),
    ])
    c.executemany("INSERT INTO sde_type_effects (type_id,effect_id,is_default) VALUES (?,?,?)", [
        (100, 900, 0), (100, 903, 0),
        (101, 901, 0), (101, 903, 0),
        (102, 902, 0),
        (103, 901, 0),
    ])
    c.executemany("INSERT INTO sde_modifiers (effect_id,func,domain,modified_attribute_id,modifying_attribute_id,operator,filter_type,filter_value) VALUES (?,?,?,?,?,?,?,?)", [
        (900, "OwnerRequiredSkillModifier", "charID", 114, 478, 6, "skill", 3324),
        (901, "OwnerRequiredSkillModifier", "charID", 114, 478, 6, "skill", 3306),   # turret skill on a missile hull
        (902, "OwnerRequiredSkillModifier", "charID", 114, 478, 6, "skill", 3436),
        (903, "LocationRequiredSkillModifier", "shipID", 265, 478, 6, "skill", 3394),  # non-weapon: never screened
    ])
    c.commit()
    return c


def test_screen_flags_only_a_wrong_weapon_system():
    audit = _load().audit
    result = audit(_seed())
    assert result["ships_audited"] == 3, "unpublished hull is not audited"
    flagged = {(s["ship_name"], s["known_correct"]) for s in result["suspects"]}
    assert flagged == {("Broken Missile Boat", False), ("Some Hauler", True)}
    broken = next(s for s in result["suspects"] if s["ship_name"] == "Broken Missile Boat")
    assert broken["skill_name"] == "Medium Energy Turret"
    assert broken["family"] == "gunnery" and broken["hull_families"] == ["missiles"]


def test_main_exit_status_gates_on_unexplained_flags(capsys):
    mod = _load()
    c = _seed()
    path = c.execute("PRAGMA database_list").fetchone()[2]
    c.close()
    assert mod.main(["audit", path]) == 1, "an unexplained flag fails the run"
    # Fix the broken hull's row and the run passes with only the known-correct flag.
    c = sqlite3.connect(path)
    c.execute("UPDATE sde_modifiers SET filter_value = 3324 WHERE effect_id = 901")
    c.commit(); c.close()
    assert mod.main(["audit", path]) == 0
