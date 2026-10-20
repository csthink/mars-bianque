"""The Alertmanager webhook: alerts start investigations.

The webhook takes a bearer token when ``BIANQUE_ALERTMANAGER_TOKEN`` is set. Each firing
alert starts an investigation unless one of the same fingerprint has not ended yet; a
resolved alert starts nothing. The investigations run in the background, so the answer
only says what became of each alert.
"""

from __future__ import annotations

import hmac
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from bianque.agents.service import InvestigationService
from bianque.api.alertmanager import AlertFormatError, parse_webhook
from bianque.api.schemas import DeliveredAlertView, WebhookResult
from bianque.config import Settings

router = APIRouter(prefix="/api/alerts", tags=["alerts"])


def _check_token(request: Request) -> None:
    settings: Settings = request.app.state.settings
    if settings.alertmanager_token is None:
        return
    expected = f"Bearer {settings.alertmanager_token.get_secret_value()}"
    given = request.headers.get("authorization", "")
    if not hmac.compare_digest(given.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="invalid webhook token")


@router.post(
    "/alertmanager",
    status_code=202,
    response_model=WebhookResult,
    dependencies=[Depends(_check_token)],
)
async def alertmanager(request: Request, body: Annotated[dict[str, Any], Body()]) -> WebhookResult:
    """Receive an Alertmanager webhook delivery; the token is checked before the body."""
    try:
        alerts = parse_webhook(body)
    except AlertFormatError:
        raise HTTPException(status_code=422, detail="not an Alertmanager webhook") from None
    service: InvestigationService = request.app.state.investigations
    delivered = await service.deliver(alerts)
    return WebhookResult(
        alerts=[
            DeliveredAlertView(
                fingerprint=item.alert.fingerprint,
                status=item.alert.status,
                investigation_id=item.investigation_id,
                deduplicated=item.deduplicated,
            )
            for item in delivered
        ]
    )
