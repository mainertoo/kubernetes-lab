"""Ephemeral TURN credentials for coturn's --use-auth-secret mode.

    username   = "<unix-expiry>:<label>"
    credential = base64(HMAC-SHA1(secret, username))

coturn recomputes the HMAC and rejects expired usernames; nothing is stored on
either side and no static secret ever reaches the browser.
"""

import base64
import hashlib
import hmac
import time


def mint_turn_credentials(
    secret: str,
    uris: list[str],
    ttl_seconds: int = 7200,
    label: str = "beam",
    now: float | None = None,
) -> dict:
    if not secret:
        raise ValueError("TURN secret is empty — TURN is disabled")
    expiry = int(now if now is not None else time.time()) + ttl_seconds
    username = f"{expiry}:{label}"
    digest = hmac.new(secret.encode(), username.encode(), hashlib.sha1).digest()
    return {
        "username": username,
        "credential": base64.b64encode(digest).decode(),
        "ttl": ttl_seconds,
        "uris": uris,
    }
