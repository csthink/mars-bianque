"""Login, logout and the session endpoint."""

from __future__ import annotations

import time
from collections.abc import Iterator
from http.cookies import SimpleCookie

import pytest
from demo_login import (
    BASE_URL,
    PASSWORD,
    PASSWORD_HASH,
    SECRET,
    USERNAME,
    HTTPResponse,
    settings,
)
from fastapi.testclient import TestClient

from bianque.api.app import create_app
from bianque.auth.sessions import COOKIE_NAME, issue_token


@pytest.fixture
def client(pg_url: str) -> Iterator[TestClient]:
    with TestClient(create_app(settings(pg_url)), base_url=BASE_URL) as test_client:
        yield test_client


def _set_cookie(response: HTTPResponse) -> SimpleCookie:
    cookie: SimpleCookie = SimpleCookie()
    cookie.load(response.headers["set-cookie"])
    return cookie


def test_login_sets_a_session_cookie(client: TestClient) -> None:
    response = client.post("/api/auth/login", json={"username": USERNAME, "password": PASSWORD})
    assert response.status_code == 200
    assert response.json() == {"username": USERNAME}
    morsel = _set_cookie(response)[COOKIE_NAME]
    assert morsel["httponly"] is True
    assert morsel["secure"] is True
    assert morsel["samesite"] == "strict"
    assert morsel["path"] == "/api"
    assert morsel["max-age"] == "43200"
    assert client.get("/api/auth/session").json() == {"username": USERNAME}


def test_cookie_without_secure_when_configured(pg_url: str) -> None:
    app = create_app(settings(pg_url, session_cookie_secure=False, session_ttl_seconds=600))
    with TestClient(app, base_url="http://testserver") as client:
        response = client.post("/api/auth/login", json={"username": USERNAME, "password": PASSWORD})
        morsel = _set_cookie(response)[COOKIE_NAME]
        assert morsel["secure"] == "" and morsel["max-age"] == "600"
        assert client.get("/api/auth/session").status_code == 200


@pytest.mark.parametrize(
    "body",
    [
        {"username": USERNAME, "password": "wrong"},
        {"username": "nobody", "password": PASSWORD},
        {"username": USERNAME, "password": "x" * 100},
    ],
    ids=["wrong-password", "unknown-user", "over-72-bytes"],
)
def test_failed_login_is_one_answer(client: TestClient, body: dict[str, str]) -> None:
    response = client.post("/api/auth/login", json=body)
    assert response.status_code == 401
    assert response.json() == {"detail": "invalid username or password"}
    assert "set-cookie" not in response.headers
    assert client.get("/api/auth/session").status_code == 401


@pytest.mark.parametrize(
    "body",
    [{"username": USERNAME}, {"password": PASSWORD}, {"username": "x" * 65, "password": "p"}],
    ids=["no-password", "no-username", "long-username"],
)
def test_malformed_login_is_422(client: TestClient, body: dict[str, str]) -> None:
    assert client.post("/api/auth/login", json=body).status_code == 422


def test_session_before_login_is_401(client: TestClient) -> None:
    response = client.get("/api/auth/session")
    assert response.status_code == 401
    assert response.json() == {"detail": "login required"}


def test_logout_clears_the_cookie(client: TestClient) -> None:
    client.post("/api/auth/login", json={"username": USERNAME, "password": PASSWORD})
    response = client.post("/api/auth/logout")
    assert response.status_code == 204
    morsel = _set_cookie(response)[COOKIE_NAME]
    assert morsel["max-age"] == "0" and morsel["path"] == "/api"
    assert client.get("/api/auth/session").status_code == 401
    # Logging out without a session is not an error.
    assert client.post("/api/auth/logout").status_code == 204


def test_expired_session_is_401(client: TestClient) -> None:
    old = issue_token(USERNAME, PASSWORD_HASH, secret=SECRET.encode(), now=time.time() - 43200)
    client.cookies.set(COOKIE_NAME, old, domain="testserver", path="/api")
    assert client.get("/api/auth/session").status_code == 401


def test_no_accounts_admits_nobody(pg_url: str) -> None:
    app = create_app(settings(pg_url, demo_users={}, session_secret=None))
    with TestClient(app, base_url=BASE_URL) as client:
        response = client.post("/api/auth/login", json={"username": USERNAME, "password": PASSWORD})
        assert response.status_code == 401
