"""Sessions end when the account behind them changes.

The session cookie is signed but stateless, and routes identify the user by
`request.session["user_id"]` alone. Without a check against the database, a
cookie outlived everything that should have ended it: removing the account,
changing its role, or a new signup being given the same id.

Each user row carries a random `session_epoch`, copied into the cookie at
sign-in. `check_session` runs before every route (an app-wide dependency in
app/main.py) and clears the session when its user no longer exists or its epoch
is not the row's current one. Rotating the epoch therefore signs the account
out everywhere.

It is a dependency rather than middleware so it goes through `get_db`, the same
database session the route itself uses.
"""
from __future__ import annotations

import secrets

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User, get_db

SESSION_EPOCH_KEY = "session_epoch"


def new_session_epoch() -> str:
    return secrets.token_hex(16)


def rotate_session_epoch(user: User) -> None:
    """End every session this account has. Caller commits."""
    user.session_epoch = new_session_epoch()


async def check_session(request: Request, db: AsyncSession = Depends(get_db)) -> None:
    user_id = request.session.get("user_id")
    if user_id is None:
        return
    row = (await db.execute(select(User.session_epoch).where(User.id == user_id))).first()
    # Release the connection: many routes never touch the database again, and
    # an open read transaction held across a slow ESI call pins the WAL.
    await db.rollback()
    if row is None or request.session.get(SESSION_EPOCH_KEY) != row[0]:
        request.session.clear()
