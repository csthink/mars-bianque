"""The Alertmanager webhook: deduplication by fingerprint, resolved alerts, the token."""

from __future__ import annotations

import json
import secrets
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
from live_server import CASES, Hold, Server, scripted, serve
from sqlalchemy import Engine, func, select, text

from bianque.storage.models import Alert, Investigation

WEBHOOK = "/api/alerts/alertmanager"


def webhook(status: str = "firing") -> dict[str, Any]:
    body: dict[str, Any] = json.loads((CASES / "s1-a" / "pack" / "alert.json").read_text())
    for alert in body["alerts"]:
        alert["status"] = status
    return body


def deliver(server: Server, body: dict[str, Any] | None = None, **headers: str) -> httpx.Response:
    return server.client.post(WEBHOOK, json=webhook() if body is None else body, headers=headers)


def count(migrated: Engine, model: Any) -> int:
    with migrated.connect() as connection:
        return int(connection.scalar(select(func.count()).select_from(model)) or 0)


def test_a_repeated_delivery_while_investigating_starts_nothing(
    migrated: Engine, pg_url: str
) -> None:
    hold = Hold("investigation")
    with serve(scripted(pg_url), rules=[hold.rule], model_sleep=hold.sleep) as server:
        first = deliver(server)
        assert first.status_code == 202
        [created] = first.json()["alerts"]
        assert created["deduplicated"] is False and created["investigation_id"]
        hold.wait_reached()
        received = server.investigation(created["investigation_id"])["alert"]["received_at"]

        again = deliver(server).json()["alerts"]

        assert again == [{**created, "investigation_id": None, "deduplicated": True}]
        detail = server.investigation(created["investigation_id"])
        assert detail["alert"]["received_at"] > received
        assert count(migrated, Investigation) == 1 and count(migrated, Alert) == 1
        hold.released.set()
        server.wait_until(created["investigation_id"], "reported")

        # A finished investigation is over: the same alert starts a new one.
        later = deliver(server).json()["alerts"][0]
        assert later["deduplicated"] is False and later["investigation_id"]
        assert later["investigation_id"] != created["investigation_id"]
        server.wait_until(later["investigation_id"], "reported")


def test_concurrent_deliveries_of_one_alert_start_one_investigation(
    migrated: Engine, pg_url: str
) -> None:
    hold = Hold("investigation")
    with serve(scripted(pg_url), rules=[hold.rule], model_sleep=hold.sleep) as server:
        with ThreadPoolExecutor(8) as pool:
            answers = list(pool.map(lambda _: deliver(server).json()["alerts"][0], range(8)))
        started = [a["investigation_id"] for a in answers if a["investigation_id"]]
        assert len(started) == 1
        assert sum(a["deduplicated"] for a in answers) == 7
        assert count(migrated, Investigation) == 1
        hold.released.set()
        server.wait_until(started[0], "reported")


def test_a_resolved_alert_starts_nothing_and_is_not_stored(migrated: Engine, pg_url: str) -> None:
    with serve(scripted(pg_url)) as server:
        [answer] = deliver(server, webhook("resolved")).json()["alerts"]
    assert answer["status"] == "resolved"
    assert answer["investigation_id"] is None and answer["deduplicated"] is False
    assert count(migrated, Alert) == 0 and count(migrated, Investigation) == 0


def test_a_webhook_investigation_reads_the_configured_pack(migrated: Engine, pg_url: str) -> None:
    with serve(scripted(pg_url)) as server:
        [answer] = deliver(server).json()["alerts"]
        detail = server.wait_until(answer["investigation_id"], "reported")
    info = detail["investigation"]
    assert info["trigger"] == "alert" and info["case_id"] == "s1-a"
    assert detail["report"]["body"]["root_cause"]["status"] == "identified"


def test_a_malformed_body_is_rejected_and_nothing_is_stored(migrated: Engine, pg_url: str) -> None:
    with serve(scripted(pg_url)) as server:
        assert deliver(server, {"alerts": []}).status_code == 422
        assert deliver(server, {"alerts": [{"status": "firing"}]}).status_code == 422
        assert server.client.post(WEBHOOK, content=b"not json").status_code == 422
    assert count(migrated, Alert) == 0


def test_a_configured_token_is_required(migrated: Engine, pg_url: str) -> None:
    token = secrets.token_hex(16)
    with serve(scripted(pg_url, alertmanager_token=token)) as server:
        assert deliver(server).status_code == 401
        assert deliver(server, Authorization="Bearer wrong").status_code == 401
        assert deliver(server, Authorization=token).status_code == 401
        # The token is checked before the body is read.
        assert deliver(server, {"alerts": []}).status_code == 401
        accepted = deliver(server, Authorization=f"Bearer {token}")
        assert accepted.status_code == 202
        server.wait_until(accepted.json()["alerts"][0]["investigation_id"], "reported")
    with migrated.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM investigations")) == 1
