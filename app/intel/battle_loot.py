"""Battle report loot finder (T-082): which wrecks of a fight are worth looting.

A pasted battle link is *parsed, never fetched*: every supported link reduces
to a Scope, a short list of (system, start, end) windows, and the loader reads
those windows from the local `killmails` / `killmail_items` archive. The one
exception is a saved br.evetools.org report, which app/intel/evetools_br.py
resolves to windows once, at paste time.

  parse_br_link      pasted text -> BrRef (host/path allow-list, ints only)
  scope_from_related BrRef fields -> Scope
  parse_windows      the canonical `w=sys:start:end` query -> Scope
  load_scope         Scope -> kills (one INDEXED BY range scan per window)
  load_items         kill ids -> item rows, chunked
  value_wrecks       pure: kills + items + prices -> wreck rows and totals

Nothing here touches the network. Names and ids in the pasted text are never
echoed back: the parser emits ints and a 24-hex id only.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import unquote, urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.db.sqlite_hints  # noqa: F401  (makes with_hint(dialect_name="sqlite") real)
from app.db.models import Killmail, KillmailItem

HOSTS_ZKILL = {"zkillboard.com", "www.zkillboard.com"}
HOSTS = HOSTS_ZKILL | {"killmail.app", "br.evetools.org", "kb.evetools.org"}

MAX_URL_LEN = 2000
DEFAULT_HOURS_BEFORE, DEFAULT_HOURS_AFTER = 1, 2
HOURS_MIN, HOURS_MAX = 1, 12
MAX_WINDOWS = 10
MAX_WINDOW_SECS = 24 * 3600
MAX_KILLS = 5000
ITEM_CHUNK = 500
SYSTEM_TIME_INDEX = "ix_killmail_system_time"

# The earliest year a stamp may name (EVE launched in 2003).
_MIN_YEAR = 2003
_MAX_YEAR = 2100
# Bounds for crafted input: ids that can't be an EVE system, or a time past
# 2100, are refused before they reach SQLite or datetime.
MAX_SYSTEM_ID = 2_147_483_647
MAX_EPOCH = 4_102_444_800

_RELATED_RE = re.compile(r"^/related/(\d{1,10})/(\d{12})(?:/o/([^/]{1,1800}))?/?$")
_EVETOOLS_BR_RE = re.compile(r"^/br/([0-9a-f]{24})/?$")
_EVETOOLS_BATTLE_RE = re.compile(r"^/battle/(\d{1,10})/?$")
_WINDOW_RE = re.compile(r"^(\d{1,10}):(\d{9,11}):(\d{9,11})$")
SRC_RE = re.compile(r"^(?:evetools:[0-9a-f]{24}|evetools-battle:\d{1,10})$")

SORRY_PASTE_RELATED = ("Couldn't read that evetools report right now. Open it on evetools "
                       "and paste each system's 'link to related' instead.")


class BrError(ValueError):
    """A pasted link (or window query) that can't be used; the message is shown
    to the user, so it never contains any of the pasted text."""


@dataclass(frozen=True)
class Window:
    system_id: int
    start: int  # epoch seconds, inclusive
    end: int    # epoch seconds, inclusive


@dataclass(frozen=True)
class BrRef:
    kind: str                      # "related" | "evetools_br" | "evetools_battle"
    system_ids: tuple[int, ...] = ()   # related: primary first, then extras
    stamp: int = 0                 # related: epoch seconds, floored to the hour
    hours_before: int = DEFAULT_HOURS_BEFORE
    hours_after: int = DEFAULT_HOURS_AFTER
    br_id: str = ""                # evetools_br
    battle_id: int = 0             # evetools_battle


Scope = tuple[Window, ...]


def _epoch_to_naive(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, timezone.utc).replace(tzinfo=None)


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def parse_stamp(digits: str) -> int:
    """YYYYMMDDHHMM -> epoch seconds. zKill requires minutes `00` and sends
    anything else to the hour, so the minutes are floored here too."""
    try:
        dt = datetime(int(digits[0:4]), int(digits[4:6]), int(digits[6:8]),
                      int(digits[8:10]), 0, tzinfo=timezone.utc)
        if int(digits[10:12]) > 59:
            raise ValueError
    except ValueError:
        raise BrError("That link's timestamp isn't a valid date.") from None
    if not _MIN_YEAR <= dt.year <= _MAX_YEAR:
        raise BrError("That link's timestamp isn't a valid date.")
    return int(dt.timestamp())


def _hours(v, default: int) -> int:
    """zKill takes these through intval, so a digit string is fine."""
    if v is None:
        return default
    if isinstance(v, str) and v.isascii() and v.isdigit() and len(v) <= 6:
        v = int(v)
    if not _is_int(v):
        raise BrError("That link's time range isn't valid.")
    return max(HOURS_MIN, min(HOURS_MAX, v))


def _parse_options(raw: str, primary: int) -> tuple[tuple[int, ...], int, int]:
    """zKill's `/o/<urlencoded json>` suffix, read the way zKill reads it: a
    `systems` list IS the selected set (the path system is replaced if it isn't
    listed) and is cut to 10. Side lists are ignored."""
    try:
        doc = json.loads(unquote(raw))
    except (ValueError, RecursionError):
        raise BrError("That link's options aren't readable.") from None
    if not isinstance(doc, dict):
        raise BrError("That link's options aren't readable.")
    systems = [primary]
    listed = doc.get("systems")
    if listed is not None:
        if not isinstance(listed, list):
            raise BrError("That link lists a system id that isn't valid.")
        chosen: list[int] = []
        for s in listed[:MAX_WINDOWS]:
            if not _is_int(s) or not 0 < s <= MAX_SYSTEM_ID:
                raise BrError("That link lists a system id that isn't valid.")
            if s not in chosen:
                chosen.append(s)
        if chosen:
            systems = chosen
    return (tuple(systems), _hours(doc.get("hoursBefore"), DEFAULT_HOURS_BEFORE),
            _hours(doc.get("hoursAfter"), DEFAULT_HOURS_AFTER))


def parse_br_link(text: str) -> BrRef:
    """Validate a pasted battle link and reduce it to ints (and a 24-hex id).

    Raises BrError with a user-facing reason. Host matching is exact: no
    suffix match, trailing dot, IP literal, userinfo or port. The query string
    and fragment are ignored."""
    text = (text or "").strip()
    if not text:
        raise BrError("Paste a battle report link.")
    if len(text) > MAX_URL_LEN:
        raise BrError("That link is too long.")
    if any(ord(c) < 33 or ord(c) == 127 for c in text):
        raise BrError("That doesn't look like a link.")
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        raise BrError("That doesn't look like a link.") from None
    host = parts.hostname or ""
    if (parts.scheme not in ("http", "https") or port is not None
            or parts.netloc.lower() != host or host not in HOSTS):
        raise BrError("Only zKillboard, killmail.app and evetools battle links are supported.")

    path = parts.path
    if path.startswith("/kill/"):
        raise BrError("That's a single killmail. Paste a battle's 'related' link instead.")
    if host in HOSTS_ZKILL | {"killmail.app"} and path.startswith("/br/"):
        raise BrError("Saved reports on that site can't be read. Paste its 'related' link instead.")

    if host == "br.evetools.org":
        m = _EVETOOLS_BR_RE.match(path)
        if m:
            return BrRef(kind="evetools_br", br_id=m.group(1))
        m = _EVETOOLS_BATTLE_RE.match(path)
        if m:
            return BrRef(kind="evetools_battle", battle_id=int(m.group(1)))

    m = _RELATED_RE.match(path)
    if not m:
        raise BrError("That doesn't look like a battle 'related' link.")
    system_id, stamp_digits, opts = int(m.group(1)), m.group(2), m.group(3)
    if not 0 < system_id <= MAX_SYSTEM_ID:
        raise BrError("That link names a system that isn't valid.")
    stamp = parse_stamp(stamp_digits)
    systems, hb, ha = (system_id,), DEFAULT_HOURS_BEFORE, DEFAULT_HOURS_AFTER
    if opts is not None:
        if host not in HOSTS_ZKILL:
            raise BrError("That link has options this site doesn't use.")
        systems, hb, ha = _parse_options(opts, system_id)
    return BrRef(kind="related", system_ids=systems, stamp=stamp,
                 hours_before=hb, hours_after=ha)


def normalize_scope(windows) -> Scope:
    """Dedupe, sort and bound a window list. Raises BrError."""
    out = sorted(set(windows), key=lambda w: (w.start, w.system_id, w.end))
    if not out:
        raise BrError("No time window to look at.")
    if len(out) > MAX_WINDOWS:
        raise BrError(f"A battle report can cover at most {MAX_WINDOWS} systems.")
    for w in out:
        if (not 0 < w.system_id <= MAX_SYSTEM_ID or w.start < 0 or w.end > MAX_EPOCH
                or w.start >= w.end or w.end - w.start > MAX_WINDOW_SECS):
            raise BrError("That time window isn't valid.")
    return tuple(out)


def scope_from_related(system_ids, stamp: int, hours_before: int = DEFAULT_HOURS_BEFORE,
                       hours_after: int = DEFAULT_HOURS_AFTER) -> Scope:
    hb = max(HOURS_MIN, min(HOURS_MAX, hours_before))
    ha = max(HOURS_MIN, min(HOURS_MAX, hours_after))
    return normalize_scope(Window(s, stamp - hb * 3600, stamp + ha * 3600) for s in system_ids)


def parse_windows(raw: list[str]) -> Scope:
    """The canonical `w=<system>:<start>:<end>` query values -> Scope."""
    windows = []
    for item in raw[: MAX_WINDOWS + 1]:
        m = _WINDOW_RE.match(item)
        if not m:
            raise BrError("That time window isn't valid.")
        windows.append(Window(int(m.group(1)), int(m.group(2)), int(m.group(3))))
    return normalize_scope(windows)


def window_params(scope: Scope) -> list[str]:
    return [f"{w.system_id}:{w.start}:{w.end}" for w in scope]


def scope_key(scope: Scope, src: str = "") -> str:
    return "|".join(window_params(scope)) + (f"#{src}" if src else "")


def in_scope(scope: Scope, system_id: int, when: datetime) -> bool:
    ts = int(when.replace(tzinfo=timezone.utc).timestamp())
    return any(w.system_id == system_id and w.start <= ts <= w.end for w in scope)


# ── Loading ──────────────────────────────────────────────────────────────

_KILL_COLS = (Killmail.killmail_id, Killmail.killmail_time, Killmail.solar_system_id,
              Killmail.victim_character_id, Killmail.victim_corporation_id,
              Killmail.victim_alliance_id, Killmail.victim_ship_type_id,
              Killmail.total_value)


def window_statement(w: Window):
    """One window's kills: an INDEXED BY range scan on (system, time), both ends
    inclusive. Without the hint the planner has picked the time index and
    scanned every kill in the window across the universe (see
    tests/test_killfeed_search_index_hint.py for the same incident)."""
    return (select(*_KILL_COLS)
            .with_hint(Killmail, f"INDEXED BY {SYSTEM_TIME_INDEX}", dialect_name="sqlite")
            .where(Killmail.solar_system_id == w.system_id,
                   Killmail.killmail_time >= _epoch_to_naive(w.start),
                   Killmail.killmail_time <= _epoch_to_naive(w.end))
            .order_by(Killmail.total_value.desc())
            .limit(MAX_KILLS + 1))


async def load_scope(db: AsyncSession, scope: Scope) -> tuple[list[dict], bool]:
    """Kills in every window, unioned and deduped on killmail_id.

    Returns (kills, truncated). Above MAX_KILLS the highest-value kills are
    kept."""
    seen: dict[int, dict] = {}
    for w in scope:
        for row in (await db.execute(window_statement(w))).all():
            if row.killmail_id in seen:
                continue
            seen[row.killmail_id] = {
                "killmail_id": row.killmail_id, "time": row.killmail_time,
                "system_id": row.solar_system_id, "char_id": row.victim_character_id,
                "corp_id": row.victim_corporation_id, "alliance_id": row.victim_alliance_id,
                "ship_type_id": row.victim_ship_type_id, "total_value": row.total_value or 0.0,
            }
    kills = sorted(seen.values(), key=lambda k: (-k["total_value"], k["killmail_id"]))
    truncated = len(kills) > MAX_KILLS
    return kills[:MAX_KILLS], truncated


async def load_items(db: AsyncSession, killmail_ids: list[int]) -> dict[int, list[tuple]]:
    """killmail_id -> [(type_id, destroyed, dropped, singleton, flag)], chunked
    so the IN list stays under SQLite's variable limit."""
    out: dict[int, list[tuple]] = {}
    for i in range(0, len(killmail_ids), ITEM_CHUNK):
        chunk = killmail_ids[i:i + ITEM_CHUNK]
        rows = await db.execute(
            select(KillmailItem.killmail_id, KillmailItem.item_type_id,
                   KillmailItem.quantity_destroyed, KillmailItem.quantity_dropped,
                   KillmailItem.singleton, KillmailItem.flag)
            .where(KillmailItem.killmail_id.in_(chunk)))
        for r in rows.all():
            out.setdefault(r.killmail_id, []).append(
                (r.item_type_id, r.quantity_destroyed or 0, r.quantity_dropped or 0,
                 bool(r.singleton), r.flag))
    return out


# ── Valuation (pure) ─────────────────────────────────────────────────────

THIN_ASK_RATIO = 5.0

# source tags, in the order the summary line lists them
SOURCES = ("jita", "avg", "avg_no_jita", "avg_thin", "avg_fetch_failed",
           "avg_budget", "blueprint", "unpriced")


def price_for_drop(type_id: int, avg: dict, jita: dict, blueprints: set) -> tuple[float, str, float | None]:
    """(unit price, source, alt) for one dropped type.

    `jita` maps type_id -> (status, price) with status ok | none | error |
    budget; a type absent from it is simply not Jita-priced (outside the cap,
    or not fetched yet) and takes the EVE average. `alt` is the Jita ask that a
    thin-ask guard discarded, so the UI can show both."""
    if type_id in blueprints:
        return 0.0, "blueprint", None
    a = avg.get(type_id) or 0.0
    status, price = jita.get(type_id, (None, None))
    if status == "ok" and price:
        if a and price > THIN_ASK_RATIO * a:
            return a, "avg_thin", price
        return price, "jita", None
    if not a:
        return 0.0, "unpriced", None
    source = {"none": "avg_no_jita", "error": "avg_fetch_failed", "budget": "avg_budget"}.get(status, "avg")
    return a, source, None


def group_key(kill: dict) -> str:
    """Victim group: alliance, else corp."""
    if kill.get("alliance_id"):
        return f"a:{kill['alliance_id']}"
    if kill.get("corp_id"):
        return f"c:{kill['corp_id']}"
    return "n:0"


def value_wrecks(kills: list[dict], items: dict[int, list[tuple]], avg: dict, jita: dict,
                 blueprints: set, min_isk: float) -> dict:
    """Dropped and destroyed value per wreck, plus totals overall and per group.

    Dropped items use price_for_drop (Jita where known); destroyed items are
    valued at the EVE average. A kill with no item rows at all is counted as
    `no_items` and contributes nothing. Rows are sorted by dropped value."""
    rows: list[dict] = []
    by_type: dict[int, dict] = {}
    no_items = 0
    for k in kills:
        rows_items = items.get(k["killmail_id"])
        if not rows_items:
            # No item rows at all: the stream stored no item data (or a pod
            # had none). Counted, shown as "items unknown", never valued.
            no_items += 1
            rows.append({"kill": k, "group": group_key(k), "dropped": 0.0, "destroyed": 0.0,
                         "drop_qty": 0, "zero_drop": False, "unknown": True, "worth": False,
                         "approx": False, "top": []})
            continue
        dropped = destroyed = 0.0
        drop_qty = 0
        lines: list[tuple] = []
        approx = False
        for type_id, q_destroyed, q_dropped, _single, _flag in rows_items or ():
            if q_destroyed and type_id not in blueprints:
                destroyed += q_destroyed * (avg.get(type_id) or 0.0)
            if not q_dropped:
                continue
            drop_qty += q_dropped
            unit, source, _alt = price_for_drop(type_id, avg, jita, blueprints)
            total = unit * q_dropped
            t = by_type.setdefault(type_id, {"qty": 0, "value": 0.0, "source": source})
            t["qty"] += q_dropped
            t["value"] += total
            dropped += total
            lines.append((type_id, q_dropped, total))
            if source not in ("jita", "blueprint"):
                approx = True
        lines.sort(key=lambda x: -x[2])
        rows.append({
            "kill": k, "group": group_key(k), "dropped": dropped, "destroyed": destroyed,
            "drop_qty": drop_qty, "zero_drop": drop_qty == 0, "unknown": False, "worth": dropped >= min_isk,
            "approx": approx, "top": lines[:3],
        })
    rows.sort(key=lambda r: (-r["dropped"], r["kill"]["killmail_id"]))
    groups: dict[str, dict] = {}
    for r in rows:
        if r["unknown"]:
            continue
        g = groups.setdefault(r["group"], {"key": r["group"], "wrecks": 0, "dropped": 0.0, "destroyed": 0.0})
        g["wrecks"] += 1
        g["dropped"] += r["dropped"]
        g["destroyed"] += r["destroyed"]
    return {
        "rows": rows,
        "groups": sorted(groups.values(), key=lambda g: (-g["dropped"], g["key"])),
        "dropped": sum(r["dropped"] for r in rows),
        "destroyed": sum(r["destroyed"] for r in rows),
        "wrecks": len(rows) - no_items, "no_items": no_items,
        # per dropped type, across the whole report: quantity, value, price source
        "by_type": by_type,
    }
