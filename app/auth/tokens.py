"""EVE SSO token revocation — used when a character is REMOVED.

EVE keeps one authorization per character per application, and revoking any
of its refresh tokens ends the whole authorization (verified on the dev
instance 2026-09-25). That is exactly right when a character is removed: its
access ends at EVE, not just in Vigilant's live database, so copies in old
backups stop working too. It is exactly wrong when permissions are merely
changed — the token just issued would die with the old one — so the update
flow never calls this (see app/auth/routes.py).

Best-effort: a failure is logged and never blocks the removal.
"""
from __future__ import annotations

import base64
import logging

from app.config import get_settings
from app.esi.client import get_http_client

logger = logging.getLogger(__name__)


def issued_to_us(access_token: str | None) -> bool:
    """True iff the token pair was issued to THIS instance's EVE application.

    The access token is a JWT whose ``azp`` claim is the client id it was
    issued to. Revocation is only ever attempted for our own tokens: the dev
    instance's database is seeded from production (scripts/dev_seed_tables.py
    copies ``characters`` whole), so a dev "change permissions" or "remove"
    would otherwise send production's refresh tokens to EVE under dev's
    credentials — and since revocation ends a character's whole authorization,
    that would cut off production. EVE should refuse it (RFC 7009 binds
    revocation to the issuing client), but this does not depend on it.
    Unparseable -> False.
    """
    import json
    try:
        payload_b64 = (access_token or "").split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        azp = json.loads(base64.urlsafe_b64decode(payload_b64)).get("azp")
    except Exception:
        return False
    return bool(azp) and azp == get_settings().eve_client_id


async def revoke_refresh_token(refresh_token: str | None) -> bool:
    """POST the token to EVE's RFC 7009 revocation endpoint. True on success."""
    if not refresh_token:
        return False
    settings = get_settings()
    credentials = base64.b64encode(
        f"{settings.eve_client_id}:{settings.eve_client_secret}".encode()
    ).decode()
    try:
        resp = await get_http_client().post(
            settings.eve_sso_revoke_url,
            headers={
                "Authorization": f"Basic {credentials}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"token_type_hint": "refresh_token", "token": refresh_token},
        )
    except Exception as exc:  # network trouble must not undo the user's change
        logger.warning("refresh-token revocation failed: %s", exc)
        return False
    if resp.status_code != 200:
        logger.warning("refresh-token revocation returned HTTP %s", resp.status_code)
        return False
    return True
