"""Fighter squadron helpers shared by the EFT and character-fit import paths
and by EFT export (ISS-136).

A fit stores one item per squadron: {"slot": "fighter", "quantity": <squadron
size>, "online": <launched>}. The sources we import from don't: the EVE client
and ESI carry per-type totals ("Name x18"), pyfa one line per squadron. These
helpers turn either shape into squadrons and back into EFT lines.
"""

_LAUNCHABLE = ("light", "support", "heavy")

# A pasted "Name x999999999" must not become hundreds of millions of squadron
# dicts. The biggest real hangar holds about two dozen squadrons, so a line
# past this keeps its remainder as ONE oversize stack (nothing dropped; the
# engine clamps an oversize squadron and warns).
MAX_SQUADRONS_PER_LINE = 64
MAX_SQUADRONS_PER_FIT = 128


def as_int(value, default: int = 1) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def split_quantity(quantity, max_size: int, max_squadrons: int = MAX_SQUADRONS_PER_LINE) -> list[int]:
    """Split a fighter total into full squadrons plus a short last one.
    18 at size 6 -> [6, 6, 6], 8 -> [6, 2]. An unknown size (0) or a total
    that already fits stays one stack. Past `max_squadrons` the remainder
    becomes one final oversize stack instead of more squadrons."""
    quantity = as_int(quantity)
    max_squadrons = max(1, max_squadrons)
    if quantity <= 0:
        return [1]
    if max_size <= 0 or quantity <= max_size:
        return [quantity]
    full, rest = divmod(quantity, max_size)
    if full + (1 if rest else 0) > max_squadrons:
        return [max_size] * (max_squadrons - 1) + [quantity - max_size * (max_squadrons - 1)]
    return [max_size] * full + ([rest] if rest else [])


_CLASS_RANK = {"light": 0, "heavy": 0, "support": 1}


def fighter_order_key(info: dict | None, name: str) -> tuple:
    """Order in which squadrons from per-type totals (ESI, which has no line
    order) take tubes: light and heavy before support, so damage squadrons
    launch first, then by name. Shared by Load and Save of an in-game fit."""
    return (_CLASS_RANK.get((info or {}).get("class"), 2), name or "")


def assign_fighter_squadrons(items: list[dict], fighter_info: dict, limits: dict | None,
                             warnings: list[str] | None = None) -> list[dict]:
    """Split fighter items into squadrons and decide which are launched.

    Walks the fighter items in order, each xN line split by its type's
    squadron size (per line, not per type, so a re-imported export keeps its
    own order). A squadron is launched (`online` True) while a tube and a slot
    of its class are free; everything else is kept as a reserve (`online`
    False), never dropped: structure fighters, a type with no known class, a
    hull with no tubes or a zero limit for the class. An item that already
    says `online: False` stays a reserve. A zero or negative quantity line is
    skipped.

    The number of squadrons is capped per line (see the constants): a capped
    line keeps its remainder as one oversize stack. The per-fit cap is soft:
    once it is reached, each further line adds at most one squadron. Either
    way `warnings`, if given, says so.

    Hangar volume is deliberately not checked: an imported fit over the hangar
    keeps every squadron and the engine and stats panel flag the overflow.
    Non-fighter items and their order are untouched.
    """
    limits = limits or {}
    tubes_left = as_int(limits.get("tubes", 0), 0)
    class_left = {c: as_int(limits.get(c, 0), 0) for c in _LAUNCHABLE}
    budget = MAX_SQUADRONS_PER_FIT
    capped = False
    out: list[dict] = []
    for item in items:
        if item.get("slot") != "fighter":
            out.append(item)
            continue
        if as_int(item.get("quantity", 1)) <= 0:
            continue
        info = fighter_info.get(item["type_id"]) or {}
        cls = info.get("class")
        max_size = as_int(info.get("max_size", 0), 0)
        parts = split_quantity(item.get("quantity", 1), max_size,
                               min(MAX_SQUADRONS_PER_LINE, max(1, budget)))
        budget -= len(parts)
        if max_size > 0 and parts[-1] > max_size:
            capped = True
        for qty in parts:
            launch = (
                item.get("online", True) is not False
                and cls in _LAUNCHABLE
                and tubes_left > 0
                and class_left[cls] > 0
            )
            if launch:
                tubes_left -= 1
                class_left[cls] -= 1
            squadron = dict(item)
            squadron["quantity"] = qty
            squadron["online"] = launch
            out.append(squadron)
    if capped and warnings is not None:
        warnings.append(
            "Too many fighter squadrons to import one by one; the rest of the "
            "quantity was kept as a single oversize squadron."
        )
    return out


def order_fighters_for_export(items: list[dict]) -> list[dict]:
    """Launched squadrons first, then reserves (stable within each), so a
    re-import that launches the first N squadrons that fit lands close to the
    same state. EFT has no launch marker, so a reserve that still has a free
    tube on the hull comes back launched."""
    launched = [i for i in items if i.get("online", True) is not False]
    reserve = [i for i in items if i.get("online", True) is False]
    return launched + reserve
