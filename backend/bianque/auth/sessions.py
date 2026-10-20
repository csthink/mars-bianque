"""Session tokens: signed, stateless, bound to the user's current password hash.

A token is ``<payload>.<signature>``, both base64url without padding. The payload is the
compact JSON ``{"u": <username>, "iat": <issue time, Unix seconds>}``; the signature is
HMAC-SHA256 over the encoded payload. The signing key is derived per user from the
session secret and the user's current password hash, so a new password or a new secret
invalidates every token issued before it. The server stores no sessions: logging out
removes the cookie from the browser, and a copied token stays valid until it expires.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from collections.abc import Mapping

#: The name of the session cookie.
COOKIE_NAME = "bianque_session"

#: How far a token's issue time may lie in the future, for clocks that differ slightly.
CLOCK_SKEW_SECONDS = 60


def _encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _signature(payload: str, username: str, password_hash: str, secret: bytes) -> bytes:
    key = hmac.new(secret, f"{username}\n{password_hash}".encode(), hashlib.sha256).digest()
    return hmac.new(key, payload.encode(), hashlib.sha256).digest()


def issue_token(username: str, password_hash: str, *, secret: bytes, now: float) -> str:
    """Return a session token for the user, issued at ``now``."""
    payload = _encode(json.dumps({"u": username, "iat": int(now)}, separators=(",", ":")).encode())
    return f"{payload}.{_encode(_signature(payload, username, password_hash, secret))}"


def verify_token(
    token: str,
    users: Mapping[str, str],
    *,
    secret: bytes,
    ttl_seconds: int,
    now: float,
) -> str | None:
    """Return the user a token was issued to, or ``None`` when it is not valid now.

    A token is valid when it parses, its user is among ``users``, its signature matches
    that user's current password hash, and it was issued less than ``ttl_seconds`` ago
    and not more than a minute in the future. The reason a token is invalid is not
    reported.
    """
    payload, sep, signature = token.partition(".")
    if not sep or "." in signature:
        return None
    try:
        claims = json.loads(_decode(payload))
        given = _decode(signature)
    except (binascii.Error, ValueError):
        return None
    if not isinstance(claims, dict):
        return None
    username, issued = claims.get("u"), claims.get("iat")
    if not isinstance(username, str) or type(issued) is not int:
        return None
    password_hash = users.get(username)
    if password_hash is None:
        return None
    if not hmac.compare_digest(given, _signature(payload, username, password_hash, secret)):
        return None
    if issued > now + CLOCK_SKEW_SECONDS or now >= issued + ttl_seconds:
        return None
    return username
