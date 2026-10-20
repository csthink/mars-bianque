"""Deciding on remediation suggestions, on the S1-a fixture investigation."""

from __future__ import annotations

import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

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
from investigation_fixture import FIXTURE, load
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bianque.api.app import create_app
from bianque.auth.passwords import hash_password
from bianque.auth.sessions import COOKIE_NAME, issue_token
from bianque.storage.models import Confirmation
from bianque.storage.repositories import (
    ConfirmationRepository,
    SuggestionRepository,
    SuggestionState,
)


@pytest.fixture
async def inv(session: AsyncSession) -> str:
    return await load(session, FIXTURE)


@pytest.fixture
def anonymous(inv: str, pg_url: str) -> Iterator[TestClient]:
    with TestClient(create_app(settings(pg_url)), base_url=BASE_URL) as test_client:
        yield test_client


@pytest.fixture
def client(anonymous: TestClient) -> TestClient:
    response = anonymous.post("/api/auth/login", json={"username": USERNAME, "password": PASSWORD})
    assert response.status_code == 200
    return anonymous


def _suggestions(client: TestClient, inv: str) -> list[dict[str, object]]:
    response = client.get(f"/api/investigations/{inv}")
    suggestions: list[dict[str, object]] = response.json()["suggestions"]
    return suggestions


def _decide(client: TestClient, inv: str, sg: object, **body: object) -> HTTPResponse:
    return client.post(f"/api/investigations/{inv}/suggestions/{sg}/decision", json=body)


async def _confirmations(session: AsyncSession) -> list[Confirmation]:
    session.expire_all()
    rows = await session.scalars(select(Confirmation).order_by(Confirmation.id))
    return list(rows)


async def test_confirm_and_reject_are_recorded(
    client: TestClient, inv: str, session: AsyncSession
) -> None:
    first, second = _suggestions(client, inv)
    assert first["state"] == second["state"] == "pending"
    before = datetime.now().astimezone()

    confirmed = _decide(client, inv, first["id"], decision="confirmed")
    rejected = _decide(
        client, inv, second["id"], decision="rejected", comment="  Roll back through Git.  "
    )

    assert confirmed.status_code == rejected.status_code == 201
    rows = await _confirmations(session)
    assert [(r.suggestion_id, r.decision, r.actor, r.comment) for r in rows] == [
        (first["id"], "confirmed", USERNAME, None),
        (second["id"], "rejected", USERNAME, "Roll back through Git."),
    ]
    assert all(r.investigation_id == inv and r.decided_at >= before for r in rows)

    # The response and the detail page show the same decision.
    body = rejected.json()
    assert body["id"] == second["id"] and body["state"] == "rejected"
    assert body["decided_by"] == USERNAME
    assert body["decision_comment"] == "Roll back through Git."
    assert datetime.fromisoformat(body["decided_at"]) == rows[1].decided_at
    after = _suggestions(client, inv)
    assert [(s["state"], s["decided_by"], s["decision_comment"]) for s in after] == [
        ("confirmed", USERNAME, None),
        ("rejected", USERNAME, "Roll back through Git."),
    ]
    assert after[1] == body


@pytest.mark.parametrize("comment", ["", "   ", None])
async def test_blank_comment_is_stored_as_none(
    client: TestClient, inv: str, session: AsyncSession, comment: str | None
) -> None:
    first = _suggestions(client, inv)[0]
    response = _decide(client, inv, first["id"], decision="confirmed", comment=comment)
    assert response.status_code == 201
    assert [r.comment for r in await _confirmations(session)] == [None]


@pytest.mark.parametrize("decision", ["confirmed", "rejected"])
async def test_second_decision_is_409(
    client: TestClient, inv: str, session: AsyncSession, decision: str
) -> None:
    first = _suggestions(client, inv)[0]
    _decide(client, inv, first["id"], decision="confirmed", comment="first")
    again = _decide(client, inv, first["id"], decision=decision, comment="second")
    assert again.status_code == 409
    assert again.json() == {
        "detail": "suggestion already decided",
        "state": "confirmed",
    }
    rows = await _confirmations(session)
    assert [(r.decision, r.comment) for r in rows] == [("confirmed", "first")]


async def test_concurrent_decisions_record_one(
    client: TestClient, inv: str, session: AsyncSession
) -> None:
    first = _suggestions(client, inv)[0]
    decisions = ["confirmed", "rejected"] * 4
    with ThreadPoolExecutor(max_workers=len(decisions)) as pool:
        responses = list(
            pool.map(lambda d: _decide(client, inv, first["id"], decision=d), decisions)
        )
    codes = sorted(r.status_code for r in responses)
    assert codes == [201] + [409] * (len(decisions) - 1)
    assert len(await _confirmations(session)) == 1


async def test_decision_after_the_check_is_409(
    client: TestClient, inv: str, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Another request records its decision after this one found the suggestion pending:
    # the unique constraint turns the second insert into a 409.
    first = _suggestions(client, inv)[0]
    await ConfirmationRepository(session).decide(
        inv, str(first["id"]), decision="rejected", actor="other"
    )
    await session.commit()
    original = SuggestionRepository.get_with_decision
    lookups: list[str] = []

    async def stale(
        self: SuggestionRepository, investigation_id: str, suggestion_id: str
    ) -> SuggestionState | None:
        # Only the first lookup misses the decision; the one after the conflict sees it.
        state = await original(self, investigation_id, suggestion_id)
        lookups.append(suggestion_id)
        if state is None or len(lookups) > 1:
            return state
        return SuggestionState(state.suggestion, None)

    monkeypatch.setattr(SuggestionRepository, "get_with_decision", stale)
    response = _decide(client, inv, first["id"], decision="confirmed")
    assert response.status_code == 409
    assert response.json()["state"] == "rejected"
    assert [r.actor for r in await _confirmations(session)] == ["other"]
    assert len(lookups) == 2


def _forged_cookies() -> dict[str, str]:
    stale = issue_token(USERNAME, PASSWORD_HASH, secret=SECRET.encode(), now=time.time() - 43200)
    rotated = issue_token(
        USERNAME, hash_password(PASSWORD, rounds=4), secret=SECRET.encode(), now=time.time()
    )
    removed = issue_token("ops", PASSWORD_HASH, secret=SECRET.encode(), now=time.time())
    fresh = issue_token(USERNAME, PASSWORD_HASH, secret=SECRET.encode(), now=time.time())
    return {
        "expired": stale,
        "password-changed": rotated,
        "user-removed": removed,
        "tampered": fresh[:-2] + ("AA" if not fresh.endswith("AA") else "BB"),
    }


@pytest.mark.parametrize("case", ["no-cookie", *_forged_cookies()])
async def test_decision_without_a_session_is_401(
    anonymous: TestClient, inv: str, session: AsyncSession, case: str
) -> None:
    if case != "no-cookie":
        anonymous.cookies.set(
            COOKIE_NAME, _forged_cookies()[case], domain="testserver", path="/api"
        )
    first = _suggestions(anonymous, inv)[0]
    response = _decide(anonymous, inv, first["id"], decision="confirmed")
    assert response.status_code == 401
    assert response.json() == {"detail": "login required"}
    # A malformed body does not get past the login check either.
    assert _decide(anonymous, inv, first["id"], decision="maybe").status_code == 401
    assert await _confirmations(session) == []


async def test_logged_out_session_cannot_decide(
    client: TestClient, inv: str, session: AsyncSession
) -> None:
    client.post("/api/auth/logout")
    first = _suggestions(client, inv)[0]
    assert _decide(client, inv, first["id"], decision="confirmed").status_code == 401
    assert await _confirmations(session) == []


def test_unknown_investigation_or_suggestion_is_404(client: TestClient, inv: str) -> None:
    missing_inv = _decide(client, "inv_missing", "sg_0000000000", decision="confirmed")
    assert missing_inv.status_code == 404
    missing_sg = _decide(client, inv, "sg_0000000000", decision="confirmed")
    assert missing_sg.status_code == 404
    assert missing_sg.json() == {"detail": "suggestion not found"}


@pytest.mark.parametrize(
    "body",
    [
        {"decision": "approved"},
        {},
        {"decision": "confirmed", "comment": "x" * 2001},
        {"decision": "confirmed", "actor": "someone-else"},
    ],
    ids=["unknown-decision", "no-decision", "long-comment", "actor-field"],
)
async def test_malformed_decision_is_422(
    client: TestClient, inv: str, session: AsyncSession, body: dict[str, object]
) -> None:
    first = _suggestions(client, inv)[0]
    assert _decide(client, inv, first["id"], **body).status_code == 422
    assert await _confirmations(session) == []


async def test_comment_at_the_limit_is_kept(
    client: TestClient, inv: str, session: AsyncSession
) -> None:
    first = _suggestions(client, inv)[0]
    comment = "x" * 2000
    response = _decide(client, inv, first["id"], decision="confirmed", comment=comment)
    assert response.status_code == 201
    assert [r.comment for r in await _confirmations(session)] == [comment]


async def test_form_body_is_not_accepted(
    client: TestClient, inv: str, session: AsyncSession
) -> None:
    first = _suggestions(client, inv)[0]
    response = client.post(
        f"/api/investigations/{inv}/suggestions/{first['id']}/decision",
        content="decision=confirmed",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 422
    assert await _confirmations(session) == []


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
def test_decisions_cannot_be_changed_or_removed(client: TestClient, inv: str, method: str) -> None:
    first = _suggestions(client, inv)[0]
    path = f"/api/investigations/{inv}/suggestions/{first['id']}/decision"
    assert client.request(method, path, json={"decision": "rejected"}).status_code == 405


def test_no_route_changes_or_removes_a_decision(pg_url: str) -> None:
    # The only write on decisions is the POST that records one. The OpenAPI
    # document lists every route of the application with its methods.
    paths = create_app(settings(pg_url)).openapi()["paths"]
    writes = {
        (method.upper(), path)
        for path, operations in paths.items()
        for method in operations
        if method in {"put", "patch", "delete"} or (method == "post" and "suggestions" in path)
    }
    assert writes == {
        ("POST", "/api/investigations/{investigation_id}/suggestions/{suggestion_id}/decision")
    }
