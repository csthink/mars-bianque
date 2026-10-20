"""``Authenticator``: checks demo account passwords and session tokens."""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable, Mapping
from functools import cached_property

from bianque.auth.passwords import hash_password, verify_password
from bianque.auth.sessions import issue_token, verify_token
from bianque.config import Settings


class Authenticator:
    """Checks demo account passwords and session tokens."""

    def __init__(
        self,
        users: Mapping[str, str],
        *,
        secret: bytes | None,
        ttl_seconds: int,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Hold the accounts and the session secret.

        Args:
            users: Username to bcrypt hash. Empty means nobody can log in.
            secret: The session signing secret; required when there are accounts.
            ttl_seconds: How long a session lasts.
            clock: The current time in Unix seconds.

        Raises:
            ValueError: There are accounts but no secret.
        """
        if users and not secret:
            raise ValueError("demo accounts need BIANQUE_SESSION_SECRET")
        self._users = dict(users)
        self._secret = secret or b""
        self.ttl_seconds = ttl_seconds
        self._clock = clock

    @classmethod
    def from_settings(cls, settings: Settings) -> Authenticator:
        """Build the authenticator of the process settings."""
        secret = settings.session_secret
        return cls(
            {name: value.get_secret_value() for name, value in settings.demo_users.items()},
            secret=None if secret is None else secret.get_secret_value().encode(),
            ttl_seconds=settings.session_ttl_seconds,
        )

    @cached_property
    def _dummy_hash(self) -> str:
        # Checked against when the username is unknown, so that an unknown user and a
        # wrong password take the same time.
        return hash_password(secrets.token_hex(16))

    def login(self, username: str, password: str) -> str | None:
        """Return a session token when the password is right, otherwise ``None``.

        It runs one bcrypt check, about 0.2 seconds; call it off the event loop.
        """
        password_hash = self._users.get(username)
        if password_hash is None:
            verify_password(password, self._dummy_hash)
            return None
        if not verify_password(password, password_hash):
            return None
        return issue_token(username, password_hash, secret=self._secret, now=self._clock())

    def user_of(self, token: str) -> str | None:
        """Return the user of a valid session token, otherwise ``None``."""
        if not self._users:
            return None
        return verify_token(
            token, self._users, secret=self._secret, ttl_seconds=self.ttl_seconds, now=self._clock()
        )
