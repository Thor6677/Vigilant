"""EVE SSO token revocation.

A refresh token Vigilant stops using is revoked at EVE rather than just
forgotten: forgetting it only removes it from the live database, while every
backup taken before still holds a working copy. Revocation is best-effort — a
failure is logged and never blocks the change the user asked for, because the
new token is already in place and the old one is gone from the live DB either
way.
"""
from __future__ import annotations

import base64
import logging

from app.config import get_settings
from app.esi.client import get_http_client

logger = logging.getLogger(__name__)


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
