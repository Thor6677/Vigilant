"""Pilot role tags and private notes (T-074).

One `CharacterTag` row per (user, character) holds up to 8 short role tags
("Cyno", "Hauler", ...) plus a single-line private note, both scoped to the
owning user only. Validation lives here as small pure functions so both the
route (app/routes/character_tags.py) and the tests can call them directly
without touching the database:

  * `parse_tags_input` / `validate_tags` — comma-separated string or list ->
    a validated, deduped tag list. Raises `TagError` (a `ValueError`) with a
    message that's safe to show the user verbatim; nothing here silently
    truncates or mangles an out-of-bounds tag.
  * `normalize_note` — free text -> a single trimmed line, or `None`.

`save_character_tags` is the only write path: it upserts the row, or deletes
it when both `tags` and `note` end up empty, so an all-empty row is never
left behind. `load_character_tags` and `user_tag_vocabulary` are the read
helpers the dashboard's tag filter (a later stream) calls.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CharacterTag

MAX_TAGS = 8
MAX_TAG_LEN = 24
MAX_NOTE_LEN = 280

# Letters, digits, space, - _ . — anything else is rejected rather than
# stripped, per T-074: a tag the user typed is either accepted as typed or
# refused with a reason, never silently altered.
_TAG_CHARS = re.compile(r"^[A-Za-z0-9 _.\-]+$")

SUGGESTED_TAGS = ("Main", "Alt", "Cyno", "Scout", "Hauler", "Indy", "PI", "Trader", "Farm", "Alpha")


class TagError(ValueError):
    """Invalid tag/note input. `str(e)` is meant to be shown to the user as-is."""


def validate_tags(tags: list[str]) -> list[str]:
    """A list of candidate tags -> validated, deduped list.

    Case-insensitive dedupe keeps the first spelling seen. Raises TagError on
    the first tag that's too long, uses a disallowed character, or if more
    than MAX_TAGS survive dedupe — never truncates or drops a bad tag quietly.
    """
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in tags:
        tag = raw.strip()
        if not tag:
            continue
        if len(tag) > MAX_TAG_LEN:
            raise TagError(f'Tag "{tag}" is too long — max {MAX_TAG_LEN} characters.')
        if not _TAG_CHARS.match(tag):
            raise TagError(
                f'Tag "{tag}" has characters that aren\'t allowed — letters, '
                "digits, spaces, and - _ . only."
            )
        key = tag.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(tag)
    if len(cleaned) > MAX_TAGS:
        raise TagError(f"At most {MAX_TAGS} tags per pilot — {len(cleaned)} given.")
    return cleaned


def parse_tags_input(raw: str | None) -> list[str]:
    """The comma-separated `tags` form field -> a validated tag list.

    A blank entry from a stray or trailing comma ("Cyno,, Hauler,") is
    dropped rather than rejected — it's a formatting artifact of the
    comma-separated field, not a tag the user typed. Everything else wrong
    with a tag (too long, bad characters, too many) raises TagError.
    """
    parts = [p.strip() for p in (raw or "").split(",")]
    return validate_tags([p for p in parts if p])


def normalize_note(raw: str | None) -> str | None:
    """Free text -> a single trimmed line, or None for an empty note.

    Newlines and any other run of whitespace collapse to a single space. An
    over-length note is rejected rather than silently truncated.
    """
    if raw is None:
        return None
    note = " ".join(raw.split())
    if not note:
        return None
    if len(note) > MAX_NOTE_LEN:
        raise TagError(f"Note is too long — max {MAX_NOTE_LEN} characters.")
    return note


def _load_tags(row: CharacterTag) -> list[str]:
    try:
        tags = json.loads(row.tags_json or "[]")
    except (TypeError, ValueError):
        return []
    return tags if isinstance(tags, list) else []


async def get_character_tag_row(db: AsyncSession, user_id: int, character_id: int) -> CharacterTag | None:
    result = await db.execute(
        select(CharacterTag).where(
            CharacterTag.user_id == user_id, CharacterTag.character_id == character_id
        )
    )
    return result.scalar_one_or_none()


async def save_character_tags(
    db: AsyncSession, user_id: int, character_id: int,
    tags: list[str], note: str | None,
) -> CharacterTag | None:
    """Upsert the (user, character) row with already-validated `tags`/`note`.

    Deletes the row instead when both end up empty, so a pilot with nothing
    set carries no row at all. Returns the resulting row, or None when it was
    deleted (or never existed). Caller commits.
    """
    row = await get_character_tag_row(db, user_id, character_id)
    if not tags and not note:
        if row is not None:
            await db.delete(row)
        return None
    if row is None:
        row = CharacterTag(user_id=user_id, character_id=character_id)
        db.add(row)
    row.tags_json = json.dumps(tags)
    row.note = note
    row.updated_at = datetime.now(timezone.utc)
    return row


async def load_character_tags(db: AsyncSession, user_id: int) -> dict[int, dict]:
    """character_id -> {"tags": [...], "note": str | None}, for this user's
    characters only. A character with no row is simply absent — callers
    treat a missing key as "no tags, no note"."""
    result = await db.execute(select(CharacterTag).where(CharacterTag.user_id == user_id))
    return {
        row.character_id: {"tags": _load_tags(row), "note": row.note}
        for row in result.scalars().all()
    }


async def user_tag_vocabulary(db: AsyncSession, user_id: int) -> list[tuple[str, int]]:
    """(tag, pilot count) across this user's characters, sorted by count
    descending then name. Feeds the dashboard's tag filter chips and this
    feature's own quick-add list of already-used tags."""
    result = await db.execute(select(CharacterTag.tags_json).where(CharacterTag.user_id == user_id))
    counts: dict[str, int] = {}
    display: dict[str, str] = {}
    for (tags_json,) in result.all():
        try:
            tags = json.loads(tags_json or "[]")
        except (TypeError, ValueError):
            continue
        if not isinstance(tags, list):
            continue
        for tag in tags:
            if not isinstance(tag, str):
                continue
            key = tag.lower()
            counts[key] = counts.get(key, 0) + 1
            display.setdefault(key, tag)
    return sorted(
        ((display[key], count) for key, count in counts.items()),
        key=lambda pair: (-pair[1], pair[0].lower()),
    )
