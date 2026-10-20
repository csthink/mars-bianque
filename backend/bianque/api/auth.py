"""Demo account login, logout and the session endpoint; ``CurrentUser`` for write endpoints.

The session cookie is ``HttpOnly``, ``SameSite=Strict`` and scoped to ``/api``; pages and
API share one origin, so another site's request never carries it. ``Secure`` follows
``BIANQUE_SESSION_COOKIE_SECURE``.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from bianque.api.schemas import LoginRequest, SessionView
from bianque.auth.authenticator import Authenticator
from bianque.auth.sessions import COOKIE_NAME
from bianque.config import Settings

router = APIRouter(prefix="/api/auth", tags=["auth"])

COOKIE_PATH = "/api"


def _authenticator(request: Request) -> Authenticator:
    authenticator: Authenticator = request.app.state.authenticator
    return authenticator


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


async def current_user(request: Request) -> str:
    """The logged-in user, from the session cookie.

    Raises:
        HTTPException: 401 when there is no valid session; the reason is not told.
    """
    token = request.cookies.get(COOKIE_NAME)
    user = None if token is None else _authenticator(request).user_of(token)
    if user is None:
        raise HTTPException(status_code=401, detail="login required")
    return user


#: A write endpoint's parameter for the logged-in user; the endpoint answers 401 without
#: a valid session, before its body is looked at.
CurrentUser = Annotated[str, Depends(current_user)]


@router.post("/login", response_model=SessionView)
async def login(body: LoginRequest, request: Request, response: Response) -> SessionView:
    """Check a demo account's password and set the session cookie."""
    authenticator = _authenticator(request)
    # One bcrypt check takes about 0.2 seconds: off the event loop.
    token = await asyncio.to_thread(authenticator.login, body.username, body.password)
    if token is None:
        raise HTTPException(status_code=401, detail="invalid username or password")
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=authenticator.ttl_seconds,
        path=COOKIE_PATH,
        secure=_settings(request).session_cookie_secure,
        httponly=True,
        samesite="strict",
    )
    return SessionView(username=body.username)


@router.post("/logout", status_code=204)
async def logout(request: Request) -> Response:
    """Remove the session cookie. Logging out without a session is not an error."""
    response = Response(status_code=204)
    response.delete_cookie(
        COOKIE_NAME,
        path=COOKIE_PATH,
        secure=_settings(request).session_cookie_secure,
        httponly=True,
        samesite="strict",
    )
    return response


@router.get("/session", response_model=SessionView)
async def session(user: CurrentUser) -> SessionView:
    """Who is logged in; 401 when nobody is. Pages cannot read the HttpOnly cookie."""
    return SessionView(username=user)
