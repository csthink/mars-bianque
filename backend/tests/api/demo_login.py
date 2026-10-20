"""A demo account for the API tests, and a client that can log in with it.

The client talks to ``https://testserver``: the session cookie carries ``Secure`` by
default, and the client's cookie jar only sends it back over HTTPS, as a browser would.
"""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from typing import Any, Protocol

from bianque.auth.passwords import hash_password
from bianque.config import Settings

USERNAME = "demo"
#: Generated per test run, never written down.
PASSWORD = secrets.token_hex(16)
SECRET = secrets.token_hex(32)
BASE_URL = "https://testserver"

#: bcrypt's lowest cost keeps the tests fast.
PASSWORD_HASH = hash_password(PASSWORD, rounds=4)


def settings(pg_url: str, **overrides: object) -> Settings:
    """Settings with the demo account and a session secret."""
    values: dict[str, object] = {
        "database_url": pg_url,
        "demo_users": {USERNAME: PASSWORD_HASH},
        "session_secret": SECRET,
    }
    values.update(overrides)
    return Settings.model_validate(values)


class HTTPResponse(Protocol):
    """What the tests read of a test client response.

    The test client answers with an ``httpx`` or an ``httpx2`` response, whichever of the
    two Starlette finds installed; both have these members.
    """

    @property
    def status_code(self) -> int: ...

    @property
    def headers(self) -> Mapping[str, str]: ...

    def json(self) -> Any: ...
