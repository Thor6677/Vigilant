"""A character whose EVE authorization is no longer shown to work leaves its
account.

Each character carries two clocks:

* ``last_authorized_at``: the last proof that its authorization works, by
  either route: an EVE SSO sign-in with it (any intent of /auth/callback that
  ends with it linked, the identity-only login included), or a token refresh
  EVE accepted (app/esi/client.py:_do_refresh).
* ``last_sso_at``: the last full sign-in with it.

run_expiry_sweep applies three rules once a day, to every character on an
account, in two phases: first it decides on every character (refreshing
quiet ones, below) and removes nothing; then it removes the ones it chose,
each only if, read again, the same rule still applies. The rules:

1. **Transfer.** The stored token's ``owner`` claim and ``owner_hash`` are both
   known and differ: the row holds another EVE account's token. Removed as the
   SSO callback removes a transfer. A refresh no longer stores such a token
   (below), so this catches rows left in that state by an earlier release,
   which stored it and left the removal to this sweep.
2. **Sign-in cap.** No sign-in for CHARACTER_SSO_CAP_DAYS (365): removed, however
   well its token works.
3. **Expiry.** Nothing shown to work for CHARACTER_AUTH_EXPIRY_DAYS (90): the
   stored token is refreshed once first. EVE accepts it: stamped and kept.
   EVE refuses it (SSO 400/401), or the refresh comes back for another EVE
   account: removed. Anything else (a network error, SSO 5xx or 429) proves
   nothing, so the character is left for the next run. That is what keeps a
   long SSO or network outage from removing anyone.

When a refresh comes back for another EVE account (the character changed
owner), _do_refresh stores none of it and raises TokenRevoked: the old account
never reads the new owner's data, and every caller sees a dead authorization.
The character keeps its old tokens and stamps, shows as needing
re-authorization with its removal date, and goes by rule 3 if nobody signs in;
a sign-in by the new owner is caught by the SSO callback's transfer guard.

A character that shares no permissions syncs nothing, so nothing else ever
refreshes its token. The sweep therefore refreshes any character that has gone
CHECK_AFTER without a proof, keeping it only on success (rule 3 still needs 90
days); a working token then never reaches the warning below. A refresh that
fails here for local reasons, a token sealed under an old SECRET_KEY or issued
to another EVE application (a dev copy of production's database), is not
tried at all: neither says anything about the authorization at EVE.

The breaker. Between the phases, the expiry and sign-in-cap removals are
counted together (transfers are not, and always go). If there are at least
HOLD_MIN of them and more than CHARACTER_EXPIRY_MAX_SHARE (0.25) of all linked
characters, none of them happens this run. That is what a restored old backup
looks like (EVE retires a refresh token once it is rotated, so every token in
an old copy is refused, and every clock in it is old), and so does a wrong
system clock. The run logs an error, writes a character_expiry_held audit row
with the counts, and records the held characters (held_character_removals);
refreshes that worked keep their stamps. Admin › Overview lists them for
review with two actions (approve_held, keep_held): "Remove these characters"
removes each one that still qualifies (still on the same account, and its
stamp has not moved forward since: a sign-in or working refresh keeps it),
and "Keep for now" drops the hold, leaving the next sweep to judge afresh.
Every sweep replaces the held set with its own result. Expect one intended
case: every clock started at the upgrade, so characters whose tokens were
already dead then all come due together 90 days later. To let removals like
that through unattended, an admin raises CHARACTER_EXPIRY_MAX_SHARE (1.0 or
more turns the breaker off) and restarts, which runs the sweep again.

Removal is the same as a transfer's: the character leaves the account with its
tokens, live state and history (app/auth/purge.py:remove_with_history,
REASON_EXPIRED; the history goes in the background purge), an audit row says
which rule, and nothing is revoked at EVE. The account is otherwise left as it
is, as every other removal but self-removal (which refuses the main) leaves it:
no other character becomes main, sessions stay signed in, and an account's
last character can go.

removal_warning says when a character is heading for removal, for the Account
page and the dashboard's needs-attention strip.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.purge import (
    REASON_EXPIRED, REASON_TRANSFER, clear_esi_cache_in_background, owner_from_token,
    remove_with_history,
)
from app.config import get_settings
from app.db.models import AdminAuditLog, AsyncSessionLocal, Character, HeldCharacterRemoval

logger = logging.getLogger(__name__)

RULE_TRANSFER = "transfer"
RULE_SSO_CAP = "sso_cap"
RULE_EXPIRY = "expiry"

# The sweep refreshes a character without a proof for this long.
CHECK_AFTER = timedelta(days=1)
# The 90-day rule's warning starts once the last proof is this old (at once
# when a sync found the token rejected). More than CHECK_AFTER plus a day, so a
# token the sweep keeps refreshing never shows it.
WARN_AFTER = timedelta(days=3)
# The breaker never holds fewer removals than this, however few characters
# the instance has.
HOLD_MIN = 3

_DETAIL = {
    RULE_TRANSFER: ("Its stored token names a new EVE owner; removed from the previous "
                    "account with its stored data"),
    RULE_SSO_CAP: ("No EVE sign-in with it for {days} days (last {when}); removed with its "
                   "stored data"),
    RULE_EXPIRY: ("Its EVE authorization had not worked for {days} days (last {when}) and EVE "
                  "refused its token; removed with its stored data"),
}


def _attr(char, name):
    return char.get(name) if isinstance(char, dict) else getattr(char, name, None)


def _as_utc(dt: datetime | None) -> datetime | None:
    """Aware UTC. SQLite hands DateTime columns back naive (they are UTC)."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _naive_utc(dt: datetime) -> datetime:
    """For a DateTime column: naive UTC, as the rest of the app stores it."""
    return _as_utc(dt).replace(tzinfo=None)


def _days(n: int) -> timedelta:
    return timedelta(days=n)


# ── The warning ──────────────────────────────────────────────────────────────

def removal_warning(char, now: datetime, *, token_dead: bool = False
                    ) -> tuple[str, datetime] | None:
    """When `char` (a Character, or a dict with the two stamps) is heading for
    removal: ``(rule, removal date)``, the earlier one if both rules apply, or
    None. Pure: `now` is the caller's, and nothing is read but the stamps.

    * RULE_EXPIRY once the authorization is known dead (`token_dead`: the sync
      found it rejected, perm_status.token_failed) or its last proof is more
      than WARN_AFTER old; removed CHARACTER_AUTH_EXPIRY_DAYS after that proof.
    * RULE_SSO_CAP within CHARACTER_SSO_WARNING_DAYS of the sign-in cap.

    A missing stamp warns of nothing, as it removes nothing.
    """
    s = get_settings()
    now = _as_utc(now)
    due: list[tuple[datetime, str]] = []
    authorized = _as_utc(_attr(char, "last_authorized_at"))
    if authorized is not None and (token_dead or now - authorized > WARN_AFTER):
        due.append((authorized + _days(s.character_auth_expiry_days), RULE_EXPIRY))
    signed_in = _as_utc(_attr(char, "last_sso_at"))
    if signed_in is not None:
        cap = signed_in + _days(s.character_sso_cap_days)
        if cap - now <= _days(s.character_sso_warning_days):
            due.append((cap, RULE_SSO_CAP))
    if not due:
        return None
    when, rule = min(due)
    return rule, when


def warning_since(rule: str, due: datetime) -> datetime:
    """When the condition behind a warning began: the last proof (90-day
    rule) or the opening of the sign-in cap's warning window."""
    s = get_settings()
    days = s.character_sso_warning_days if rule == RULE_SSO_CAP else s.character_auth_expiry_days
    return due - _days(days)


def warning_text(due: datetime, now: datetime) -> str:
    """The warning, after the character's name. A date already passed means
    the sweep has not managed to decide yet, not that it never will."""
    if _as_utc(due) <= _as_utc(now):
        return "is due to be removed unless you sign in with it at EVE"
    return f"will be removed on {due:%Y-%m-%d} unless you sign in with it at EVE"


# ── Upgrade ──────────────────────────────────────────────────────────────────

async def backfill_authorization_stamps(db: AsyncSession) -> int:
    """Start both clocks now for rows from before them, so nobody is removed
    sooner than CHARACTER_AUTH_EXPIRY_DAYS after the upgrade. Run at startup;
    a no-op once every row has both. Returns how many rows were filled."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    filled = 0
    for col in (Character.last_authorized_at, Character.last_sso_at):
        res = await db.execute(update(Character).where(col.is_(None)).values({col: now})
                               .execution_options(synchronize_session=False))
        filled = max(filled, res.rowcount or 0)
    await db.commit()
    return filled


# ── The sweep ────────────────────────────────────────────────────────────────

def _transferred(char: Character) -> bool:
    """Rule 1: the row holds another EVE account's token. A refresh never
    stores one now (_do_refresh), so this is a row an earlier release left."""
    stored = owner_from_token(char.access_token)
    return bool(char.owner_hash and stored and stored != char.owner_hash)


def _removal_rule(char: Character, now: datetime, *, refused: bool) -> str | None:
    """Which rule removes `char` now, or None. `refused`: EVE refused its
    stored token on this run's refresh, the only thing rule 3 removes on."""
    s = get_settings()
    if _transferred(char):
        return RULE_TRANSFER
    signed_in = _as_utc(char.last_sso_at)
    if signed_in is not None and now - signed_in > _days(s.character_sso_cap_days):
        return RULE_SSO_CAP
    authorized = _as_utc(char.last_authorized_at)
    if refused and authorized is not None and now - authorized > _days(s.character_auth_expiry_days):
        return RULE_EXPIRY
    return None


def _breaker_trips(removals: int, linked: int, max_share: float) -> bool:
    """Whether this many expiry and sign-in-cap removals, out of `linked`
    characters, are too many to make at once. 1.0 or more turns it off."""
    return max_share < 1.0 and removals >= HOLD_MIN and removals > max_share * linked


async def _linked(db: AsyncSession, cid: int) -> Character | None:
    return (await db.execute(select(Character).where(
        Character.character_id == cid, Character.user_id.isnot(None)))).scalar_one_or_none()


@dataclass
class _Candidate:
    """A removal phase 1 decided on, with what a hold records about it."""
    character_id: int
    rule: str
    refused: bool
    character_name: str
    user_id: int
    last_proof_at: datetime | None   # the stamp the rule was judged on


def _proof(char: Character, rule: str) -> datetime | None:
    """The stamp `rule` judges: the last sign-in for the cap, otherwise the
    last proof of any kind."""
    return char.last_sso_at if rule == RULE_SSO_CAP else char.last_authorized_at


def _rule_days(rule: str) -> int:
    s = get_settings()
    return s.character_sso_cap_days if rule == RULE_SSO_CAP else s.character_auth_expiry_days


async def _decide(factory, cid: int, now: datetime) -> tuple[_Candidate | None, str]:
    """Phase 1 for one character: refresh it if it is quiet, then decide on
    the row as the refresh left it, in a session of its own. Removes nothing.
    Returns (the removal it qualifies for or None, outcome if none)."""
    from app.auth.routes import PROBE_REVOKED, _probe_stored_token
    from app.auth.tokens import issued_to_us
    from app.db.encryption import is_fernet_shaped

    verdict = None
    async with factory() as db:
        char = await _linked(db, cid)
        if char is None:
            return None, "gone"
        authorized = _as_utc(char.last_authorized_at)
        # Not when rule 1 or 2 removes it whatever the refresh says.
        if (authorized is not None and now - authorized > CHECK_AFTER
                and _removal_rule(char, now, refused=False) is None
                and issued_to_us(char.access_token) and not is_fernet_shaped(char.refresh_token)):
            # Under the sync layer's token locks, in a session of its own; a
            # successful refresh stamps last_authorized_at (_do_refresh).
            verdict, _scopes, _error = await _probe_stored_token(db, cid)

    refused = verdict == PROBE_REVOKED
    async with factory() as db:
        char = await _linked(db, cid)
        if char is None:
            return None, "gone"
        rule = _removal_rule(char, now, refused=refused)
        if rule is None:
            return None, f"checked_{verdict}" if verdict else "kept"
        return _Candidate(cid, rule, refused, char.character_name, char.user_id,
                          _proof(char, rule)), rule


async def _remove(factory, cand: _Candidate, now: datetime) -> str:
    """Phase 2 for one character: remove it, if read again the same rule still
    applies (a sign-in since phase 1 saves it). Returns the outcome."""
    cid, rule = cand.character_id, cand.rule
    async with factory() as db:
        char = await _linked(db, cid)
        if char is None:
            return "gone"
        if _removal_rule(char, now, refused=cand.refused) != rule:
            return "changed"
        user_id = char.user_id
        last = _proof(char, rule)
        detail = _DETAIL[rule].format(days=_rule_days(rule),
                                      when=f"{last:%Y-%m-%d}" if last else "never")
        transfer = rule == RULE_TRANSFER
        patterns = await remove_with_history(
            db, char, REASON_TRANSFER if transfer else REASON_EXPIRED,
            event_type="character_transferred" if transfer else "character_expired",
            detail=detail)
        bind = db.bind
    # ISS-077: the account's cached corp contracts for its corporation.
    await clear_esi_cache_in_background(bind, None, patterns)
    logger.warning("character %s removed from user %s by the expiry sweep (%s)", cid, user_id, rule)
    return rule


async def _replace_held(factory, held: list[_Candidate], now: datetime) -> None:
    """Make the held set exactly `held` (empty: nothing is held). A character
    the last set already held under the same rule keeps its held_at."""
    async with factory() as db:
        before = {cid: (rule, at) for cid, rule, at in (await db.execute(select(
            HeldCharacterRemoval.character_id, HeldCharacterRemoval.rule,
            HeldCharacterRemoval.held_at))).all()}
        await db.execute(delete(HeldCharacterRemoval))
        for c in held:
            rule, at = before.get(c.character_id, (None, None))
            db.add(HeldCharacterRemoval(
                character_id=c.character_id, character_name=c.character_name,
                user_id=c.user_id, rule=c.rule, last_proof_at=_naive_utc(c.last_proof_at),
                held_at=at if rule == c.rule else _naive_utc(now)))
        await db.commit()


async def _record_hold(factory, held: list[_Candidate], linked: int, max_share: float,
                       now: datetime) -> None:
    """The breaker tripped: say so in the log and the audit log, and keep the
    held characters for an admin to review."""
    by_rule = {r: sum(1 for c in held if c.rule == r) for r in (RULE_EXPIRY, RULE_SSO_CAP)}
    counts = ", ".join(f"{r}={n}" for r, n in by_rule.items() if n)
    logger.error("authorization expiry sweep: holding %d removals (%s) of %d linked characters, "
                 "more than CHARACTER_EXPIRY_MAX_SHARE=%s; review them in Admin > Overview",
                 len(held), counts, linked, max_share)
    detail = (f"The expiry sweep held {len(held)} removals ({counts}) of {linked} linked "
              f"characters: more than CHARACTER_EXPIRY_MAX_SHARE={max_share} at once, as a "
              f"restored old backup or a wrong clock would cause. Review them in Admin > "
              f"Overview; to let such removals through unattended, raise "
              f"CHARACTER_EXPIRY_MAX_SHARE (1.0 or more turns this check off) and restart.")
    try:
        async with factory() as db:
            db.add(AdminAuditLog(event_type="character_expiry_held", detail=detail))
            await db.commit()
    except Exception as e:
        logger.warning("authorization expiry sweep: could not record the hold: %s: %s",
                       type(e).__name__, e)
    try:
        await _replace_held(factory, held, now)
    except Exception as e:
        logger.warning("authorization expiry sweep: could not store the held removals: %s: %s",
                       type(e).__name__, e)


async def run_expiry_sweep(now: datetime | None = None, *, session_factory=None) -> dict[str, int]:
    """Decide on every character on an account, then make the removals the
    breaker allows, one at a time, each with its own sessions and commits, so
    one failure doesn't stop the rest. Called daily by the background
    scheduler. Returns {outcome: count}, always with a `held` count."""
    factory = session_factory or AsyncSessionLocal
    now = _as_utc(now or datetime.now(timezone.utc))
    try:
        async with factory() as db:
            cids = list((await db.execute(
                select(Character.character_id).where(Character.user_id.isnot(None))
                .order_by(Character.character_id))).scalars())
    except Exception as e:
        logger.warning("authorization expiry sweep: could not list characters: %s", e)
        return {"held": 0}
    outcomes: dict[str, int] = {"held": 0}

    def count(outcome: str) -> None:
        outcomes[outcome] = outcomes.get(outcome, 0) + 1

    candidates: list[_Candidate] = []
    for cid in cids:
        try:
            cand, outcome = await _decide(factory, cid, now)
        except Exception as e:
            logger.warning("authorization expiry sweep: character %s failed: %s: %s",
                           cid, type(e).__name__, e)
            count("failed")
            continue
        if cand:
            candidates.append(cand)
        else:
            count(outcome)

    max_share = get_settings().character_expiry_max_share
    expiring = [c for c in candidates if c.rule != RULE_TRANSFER]
    if _breaker_trips(len(expiring), len(cids), max_share):
        await _record_hold(factory, expiring, len(cids), max_share, now)
        outcomes["held"] = len(expiring)
        candidates = [c for c in candidates if c.rule == RULE_TRANSFER]
    else:
        try:
            await _replace_held(factory, [], now)
        except Exception as e:
            logger.warning("authorization expiry sweep: could not clear the held removals: %s: %s",
                           type(e).__name__, e)

    for cand in candidates:
        try:
            count(await _remove(factory, cand, now))
        except Exception as e:
            logger.warning("authorization expiry sweep: removing character %s failed: %s: %s",
                           cand.character_id, type(e).__name__, e)
            count("failed")
    logger.info("authorization expiry sweep: %d characters: %s", len(cids),
                json.dumps(outcomes, sort_keys=True))
    return outcomes


# ── Held removals, for Admin › Overview ──────────────────────────────────────

_RULE_WORDS = {
    RULE_EXPIRY: "EVE refused its authorization, which had not worked for over {days} days",
    RULE_SSO_CAP: "no EVE sign-in with it for over {days} days",
}


def _still_due(char: Character | None, held: HeldCharacterRemoval) -> bool:
    """Whether a held removal may still go ahead: the character is on the
    account it was held on, and the stamp it was judged on has not moved
    forward since (a sign-in or a working refresh keeps it)."""
    if char is None or char.user_id != held.user_id:
        return False
    stamp = _as_utc(_proof(char, held.rule))
    return stamp is not None and stamp <= _as_utc(held.last_proof_at)


async def held_removals(db: AsyncSession) -> list[dict]:
    """The held set for Admin › Overview, oldest hold first. Characters no
    longer on the account they were held on are left out; one that has
    proved itself since is listed with still_due False (approving keeps it)."""
    rows = list((await db.execute(select(HeldCharacterRemoval).order_by(
        HeldCharacterRemoval.held_at, HeldCharacterRemoval.character_name))).scalars())
    if not rows:
        return []
    chars = {c.character_id: c for c in (await db.execute(select(Character).where(
        Character.character_id.in_([r.character_id for r in rows])))).scalars()}
    # Each account by its main's name, else its first character's, as Admin › Users does.
    accounts: dict[int, str] = {}
    for uid, name, is_main in (await db.execute(
            select(Character.user_id, Character.character_name, Character.is_main)
            .where(Character.user_id.in_({r.user_id for r in rows}))
            .order_by(Character.is_main.desc(), Character.character_name))).all():
        accounts.setdefault(uid, name)
    out = []
    for r in rows:
        char = chars.get(r.character_id)
        if char is None or char.user_id != r.user_id:
            continue
        out.append({
            "character_id": r.character_id,
            "character_name": char.character_name,
            "account": f"{accounts.get(r.user_id, 'User')} (account {r.user_id})",
            **_describe(r),
            "still_due": _still_due(char, r),
        })
    return out


def _describe(r: HeldCharacterRemoval) -> dict:
    """A held removal in plain words, for the admin pages."""
    return {
        "why": _RULE_WORDS[r.rule].format(days=_rule_days(r.rule)),
        "last_label": "last sign-in" if r.rule == RULE_SSO_CAP else "last worked",
        "last": f"{r.last_proof_at:%Y-%m-%d}",
        "held_since": f"{r.held_at:%Y-%m-%d %H:%M} UTC",
    }


async def held_for(db: AsyncSession, characters) -> dict[int, dict]:
    """Which of these characters (rows with character_id and user_id) are held
    on the account they are on now, described as on Overview: for Admin ›
    Users. One query."""
    on = {c.character_id: c.user_id for c in characters}
    if not on:
        return {}
    rows = (await db.execute(select(HeldCharacterRemoval).where(
        HeldCharacterRemoval.character_id.in_(list(on))))).scalars()
    return {r.character_id: _describe(r) for r in rows if on.get(r.character_id) == r.user_id}


async def held_count(db: AsyncSession) -> int:
    """How many held removals Overview would list (each still on the account
    it was held on): one count over two primary-key/unique-index lookups, for
    the site-wide admin banner."""
    return (await db.execute(
        select(func.count()).select_from(HeldCharacterRemoval).join(Character, and_(
            Character.character_id == HeldCharacterRemoval.character_id,
            Character.user_id == HeldCharacterRemoval.user_id)))).scalar() or 0


async def approve_held(bind, *, admin_id: int, ip: str | None
                       ) -> tuple[int, int, tuple[str, ...]]:
    """An admin approved the held removals. Each one that is still due
    (_still_due) is removed as the sweep would have, with a character_expired
    audit row saying so, each in its own session and commit; the rest are
    kept. Then the set is cleared. Returns (removed, kept, the corp-contract
    cache patterns for the caller to clear after its response)."""
    def session():
        return AsyncSession(bind, expire_on_commit=False)

    async with session() as db:
        rows = list((await db.execute(select(HeldCharacterRemoval))).scalars())
    removed = kept = 0
    patterns: list[str] = []
    for r in rows:
        try:
            async with session() as db:
                char = await _linked(db, r.character_id)
                if not _still_due(char, r):
                    kept += 1
                    continue
                detail = (f"Held by the expiry sweep, then approved by an admin (user {admin_id}). "
                          + _DETAIL[r.rule].format(days=_rule_days(r.rule),
                                                   when=f"{r.last_proof_at:%Y-%m-%d}"))
                patterns.extend(await remove_with_history(
                    db, char, REASON_EXPIRED, event_type="character_expired", detail=detail, ip=ip))
            removed += 1
            logger.warning("character %s removed from user %s: held expiry approved by user %s",
                           r.character_id, r.user_id, admin_id)
        except Exception as e:
            kept += 1
            logger.warning("approving held removal of character %s failed: %s: %s",
                           r.character_id, type(e).__name__, e)
    async with session() as db:
        await db.execute(delete(HeldCharacterRemoval).where(
            HeldCharacterRemoval.character_id.in_([r.character_id for r in rows])))
        await db.commit()
    return removed, kept, tuple(patterns)


async def keep_held(db: AsyncSession) -> int:
    """An admin kept the held removals for now: drop the hold, leaving the
    next sweep to judge them afresh (it may hold them again). Returns how
    many were held."""
    res = await db.execute(delete(HeldCharacterRemoval))
    await db.commit()
    return res.rowcount or 0
