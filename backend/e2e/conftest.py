"""End-to-end tests of the A2A service on the replay stack, with the official SDK's client.

``make e2e`` starts the replay stack in ``scripted`` model mode with two A2A clients of
the tests' own and runs these tests after Playwright. They talk HTTP to nginx only, as
another agent does: the Agent Card it serves names the endpoint the client then uses.

Environment (set by ``make e2e``):

- ``BIANQUE_E2E_URL``: where nginx serves the stack.
- ``BIANQUE_E2E_A2A_TOKEN_A``: the token of ``e2e-agent-a`` (namespace ``demo-s1``).
- ``BIANQUE_E2E_A2A_TOKEN_B``: the token of ``e2e-agent-b`` (a namespace with no case).
"""

from __future__ import annotations

import os

import pytest


def _required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        pytest.fail(f"{name} is not set; run these tests with make e2e", pytrace=False)
    return value


@pytest.fixture(scope="session")
def url() -> str:
    """Where the stack is served."""
    return _required("BIANQUE_E2E_URL").rstrip("/")


@pytest.fixture(scope="session")
def token_a() -> str:
    """The token of ``e2e-agent-a``."""
    return _required("BIANQUE_E2E_A2A_TOKEN_A")


@pytest.fixture(scope="session")
def token_b() -> str:
    """The token of ``e2e-agent-b``."""
    return _required("BIANQUE_E2E_A2A_TOKEN_B")
