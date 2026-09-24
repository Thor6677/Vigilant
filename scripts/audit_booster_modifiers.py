#!/usr/bin/env python3
"""Report every booster modifier the fitting engine cannot apply.

T-049. A booster is any published type with a `boosterness` attribute
(1087). Its modifier rows go through `_apply_character_modifiers` in
app/fitting/engine.py — the dispatcher implants use — which knows four
shapes: ItemModifier on the ship, ItemModifier on the character's
missileDamageMultiplier, LocationGroup/LocationRequiredSkill modifiers on
modules, and OwnerRequiredSkill modifiers on modules, drones and charges.
This script walks every booster row in a database and says which of them
fall outside that, so a new SDE cannot quietly add a booster whose bonus
the engine drops.

Each row is classified as one of:

  applied          a shape the dispatcher handles, source attribute present
  no_combat_stat   ItemModifier on a character attribute the engine has no
                   stat for (perception, drone control distance, industry
                   rates); skipped by design
  unsupported      any other func/domain, or a source attribute the booster
                   does not carry — the engine silently applies nothing

Two further checks: a side effect (an effect with
fitting_usage_chance_attribute_id) whose booster lacks that chance
attribute, and a skill filter no published module, drone or charge
requires, which matches nothing.

Known flags the screen will always list:
  - effect 3672 setBonusOre on the Serenity season booster: a
    LocationGroupModifier with domain charID whose source attribute the
    booster does not even carry. Not on Tranquility; nothing to apply.
  - effect 2791 boosterMissileExplosionCloudPenaltyFixed filters on
    Acceleration Control (3452), which no charge requires.
    app/fitting/boosters.py redirects it to Missile Launcher Operation.

Result 2026-09-24 against the live SDE: 453 boosters, 2,288 modifier rows,
1,309 applied, 978 no_combat_stat (the cerebral accelerators' attribute
rows), 1 unsupported (the Serenity row), 6 dead skill filters (2791 on the
three grades of Exile and Mindflood). All known; exit 0. Re-run after every
SDE import.

Usage
-----
    python3 scripts/audit_booster_modifiers.py /data/vigilant.db
    # or on the host, read-only, against the running container's DB:
    ssh <host> 'docker exec -u 10001 -i vigilant-app-1 python3 - /data/vigilant.db' \\
        < scripts/audit_booster_modifiers.py

Exit status 0 when every flag is on the known list, 1 otherwise, so it can
gate an SDE update.
"""
from __future__ import annotations

import json
import sqlite3
import sys

ATTR_BOOSTERNESS = 1087
ATTR_MISSILE_DAMAGE_MULTIPLIER = 212

HANDLED = {
    ("ItemModifier", "shipID"),
    ("LocationGroupModifier", "shipID"),
    ("LocationRequiredSkillModifier", "shipID"),
    ("OwnerRequiredSkillModifier", "charID"),
}

# Categories whose types a booster's skill filter can meaningfully match.
# Fighters count: the Halcyon B boosters' drone-durability effect (6665)
# carries a row filtered on Fighters (23069) that only fighters require.
FILTERABLE_CATEGORIES = {7, 8, 18, 87}   # module, charge, drone, fighter

# Flags verified correct, keyed by effect id.
KNOWN_UNSUPPORTED_EFFECTS = {3672}     # setBonusOre — Serenity only
KNOWN_DEAD_FILTERS = {(2791, 3452)}    # redirected in app/fitting/boosters.py


def audit(conn: sqlite3.Connection) -> dict:
    boosters = {
        r[0]: (r[1], r[2]) for r in conn.execute(
            """SELECT t.type_id, t.type_name, a.value
               FROM sde_types t
               JOIN sde_type_dogma_attrs a ON a.type_id = t.type_id
               WHERE a.attribute_id = ? AND t.published = 1""", (ATTR_BOOSTERNESS,))
    }
    attr_names = {r[0]: r[1] for r in conn.execute(
        "SELECT attribute_id, attribute_name FROM sde_dogma_attributes")}
    group_category = {r[0]: r[1] for r in conn.execute(
        "SELECT group_id, category_id FROM sde_groups")}

    # Skills that some module, drone or charge actually requires.
    live_skills: set[int] = set()
    for skill, group in conn.execute(
            """SELECT DISTINCT r.skill_type_id, t.group_id
               FROM sde_type_skill_reqs r JOIN sde_types t ON t.type_id = r.type_id"""):
        if group_category.get(group) in FILTERABLE_CATEGORIES:
            live_skills.add(skill)

    counts = {"applied": 0, "no_combat_stat": 0, "unsupported": 0}
    flags: list[dict] = []
    rows_seen = 0

    for booster_id, (name, _slot) in sorted(boosters.items()):
        attrs = {r[0]: r[1] for r in conn.execute(
            "SELECT attribute_id, value FROM sde_type_dogma_attrs WHERE type_id = ?",
            (booster_id,))}
        effects = list(conn.execute(
            """SELECT e.effect_id, e.effect_name, e.fitting_usage_chance_attribute_id
               FROM sde_type_effects te JOIN sde_effects e ON e.effect_id = te.effect_id
               WHERE te.type_id = ?""", (booster_id,)))
        for effect_id, effect_name, chance_attr in effects:
            if chance_attr is not None and chance_attr not in attrs:
                flags.append({
                    "kind": "missing_chance_attribute", "booster": booster_id,
                    "booster_name": name, "effect": effect_id, "effect_name": effect_name,
                    "chance_attribute": chance_attr, "known": False,
                })
            mods = conn.execute(
                """SELECT func, domain, modified_attribute_id, modifying_attribute_id,
                          filter_type, filter_value
                   FROM sde_modifiers WHERE effect_id = ?""", (effect_id,))
            for func, domain, target, source, filter_type, filter_value in mods:
                rows_seen += 1
                base = {
                    "booster": booster_id, "booster_name": name,
                    "effect": effect_id, "effect_name": effect_name,
                    "func": func, "domain": domain,
                    "target": target, "target_name": attr_names.get(target),
                    "source": source, "source_name": attr_names.get(source),
                }
                if (func, domain) == ("ItemModifier", "charID"):
                    if target != ATTR_MISSILE_DAMAGE_MULTIPLIER:
                        counts["no_combat_stat"] += 1
                        continue
                elif (func, domain) not in HANDLED:
                    counts["unsupported"] += 1
                    flags.append({"kind": "unsupported_shape", **base,
                                  "known": effect_id in KNOWN_UNSUPPORTED_EFFECTS})
                    continue
                if source not in attrs:
                    counts["unsupported"] += 1
                    flags.append({"kind": "missing_source_attribute", **base,
                                  "known": effect_id in KNOWN_UNSUPPORTED_EFFECTS})
                    continue
                if filter_type == "skill" and filter_value not in live_skills:
                    flags.append({"kind": "dead_skill_filter", **base, "skill": filter_value,
                                  "known": (effect_id, filter_value) in KNOWN_DEAD_FILTERS})
                counts["applied"] += 1

    return {
        "boosters": len(boosters),
        "modifier_rows": rows_seen,
        **counts,
        "flags": flags,
    }


def main(argv: list[str]) -> int:
    path = argv[1] if len(argv) > 1 else "/data/vigilant.db"
    conn = sqlite3.connect(path)
    result = audit(conn)
    unexplained = [f for f in result["flags"] if not f["known"]]
    print(json.dumps({**result, "unexplained": len(unexplained)}, indent=1))
    return 1 if unexplained else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
