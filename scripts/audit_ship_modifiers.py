#!/usr/bin/env python3
"""Screen every hull's SDE modifierInfo for a wrong-weapon-system skill filter.

ISS-015 / ISS-029. The documented bug class: a hull's bonus modifier filters
on a skill from a weapon system the hull does not have — the Sacrilege once
kept a Medium Energy Turret filter after CCP made it a missile boat, so the
engine applied its damage bonus to nothing. `_SHIP_MODIFIER_OVERRIDES` in
app/fitting/engine.py exists to correct such rows. This script finds them.

Method
------
For each published ship with trait rows (sde_type_bonuses), collect the
weapon-system families its traits target — from the trait's target skill's
group, and from keywords in the trait text. Then for every skill-filtered
modifier on the hull's effects whose skill belongs to a weapon family
(Gunnery, Missiles, Drones, Fighters), flag it if that family is not one the
hull's traits name.

Only weapon families are screened on purpose. The SDE legitimately filters
on a module's *required* skill, which is broader than the trait wording
(every missile hull filters on Missile Launcher Operation; probe bonuses
filter on Astrometrics), so a naive "is this skill named in the trait text"
check flags six hundred correct rows. A wrong weapon system is the thing
that actually breaks a fit.

Known-correct flags the screen will always list, because the hull carries
a bonus its traits do not print:
  - industrialBonusDroneDamage on industrials, haulers, exhumers and the
    Noctis: a real hidden CCP effect. The Drones filter is right.
  - the Zirnitra's Capital Precursor Weapon rows: the trait keywords say
    "disintegrator", which is not a Gunnery hint here.

Result 2026-09-24 against the live SDE: 419 hulls, 2,155 skill-filtered
modifiers, zero genuine mismatches. The override table stays empty. Re-run
after every SDE import; if a hull appears that is not on the known list,
verify against github.com/pyfa-org/Pyfa eos/effects/ before adding an
override.

Usage
-----
    python3 scripts/audit_ship_modifiers.py /data/vigilant.db
    # or on the host, read-only, against the running container's DB:
    ssh <host> 'docker exec -u 10001 -i vigilant-app-1 python3 - /data/vigilant.db' \\
        < scripts/audit_ship_modifiers.py

Exit status 0 when every flag is on the known-correct list, 1 otherwise, so
it can gate an SDE update.
"""
from __future__ import annotations

import json
import sqlite3
import sys

# Skill group id -> weapon-system family.
FAMILY_BY_SKILL_GROUP = {255: "gunnery", 256: "missiles", 273: "drones", 1652: "fighters"}

# Trait-text hints that a hull uses a family even where the trait's target
# skill is not in that group (e.g. a role bonus with no scaling skill).
FAMILY_HINTS = {
    "gunnery": ["turret", "laser", "hybrid", "projectile", "artillery", "autocannon",
                "railgun", "blaster", "beam", "pulse", "disintegrator", "precursor weapon"],
    "missiles": ["missile", "rocket", "torpedo", "launcher"],
    "drones": ["drone"],
    "fighters": ["fighter"],
}

# Effects the screen flags on hulls whose traits do not print the bonus, and
# which are verified correct. Keyed by effect name.
KNOWN_CORRECT_EFFECTS = {
    "industrialBonusDroneDamage",   # hidden drone bonus on industrial hulls
}


def hull_families(traits: list[tuple], types: dict[int, tuple[str, int]]) -> set[str]:
    fams: set[str] = set()
    for scaling_skill, target_skill, keyword in traits:
        for skill in (scaling_skill, target_skill):
            if skill and skill in types:
                fam = FAMILY_BY_SKILL_GROUP.get(types[skill][1])
                if fam:
                    fams.add(fam)
        text = (keyword or "").lower()
        for fam, hints in FAMILY_HINTS.items():
            if any(h in text for h in hints):
                fams.add(fam)
    return fams


def audit(conn: sqlite3.Connection) -> dict:
    types = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT type_id, type_name, group_id FROM sde_types")}
    ships = [r[0] for r in conn.execute(
        "SELECT type_id FROM sde_types WHERE category_id = 6 AND published = 1")]
    suspects: list[dict] = []
    audited = 0
    modifiers = 0
    for sid in ships:
        traits = list(conn.execute(
            "SELECT scaling_skill_id, target_type_id, bonus_keyword FROM sde_type_bonuses WHERE type_id = ?",
            (sid,)))
        if not traits:
            continue
        audited += 1
        fams = hull_families(traits, types)
        rows = conn.execute(
            """SELECT DISTINCT m.effect_id, e.effect_name, m.modifying_attribute_id, m.filter_value
               FROM sde_type_effects te
               JOIN sde_modifiers m ON m.effect_id = te.effect_id
               JOIN sde_effects e ON e.effect_id = te.effect_id
               WHERE te.type_id = ? AND m.filter_type = 'skill'""", (sid,))
        for effect_id, effect_name, mod_attr, skill in rows:
            modifiers += 1
            fam = FAMILY_BY_SKILL_GROUP.get(types.get(skill, ("", None))[1])
            if fam and fam not in fams:
                suspects.append({
                    "ship": sid, "ship_name": types[sid][0],
                    "effect": effect_id, "effect_name": effect_name,
                    "modifying_attribute_id": mod_attr,
                    "skill": skill, "skill_name": types.get(skill, ("?",))[0],
                    "family": fam, "hull_families": sorted(fams),
                    "known_correct": effect_name in KNOWN_CORRECT_EFFECTS,
                })
    return {"ships_audited": audited, "skill_modifiers": modifiers, "suspects": suspects}


def main(argv: list[str]) -> int:
    path = argv[1] if len(argv) > 1 else "/data/vigilant.db"
    conn = sqlite3.connect(path)
    result = audit(conn)
    unexplained = [s for s in result["suspects"] if not s["known_correct"]]
    print(json.dumps({**result, "unexplained": len(unexplained)}, indent=1))
    return 1 if unexplained else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
