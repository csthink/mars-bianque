"""The A2A service's parts without a database: the card, the configuration, the intake
and the texts a task carries."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from a2a.types import AgentCard, ContentTypeNotSupportedError, Part
from google.protobuf import struct_pb2
from google.protobuf.json_format import MessageToDict, ParseDict
from pydantic import ValidationError

from bianque.a2a.card import SKILL_ID, build_card
from bianque.a2a.intake import (
    REJECTIONS,
    AlertRequest,
    CaseRequest,
    Rejected,
    data_contents,
    read_request,
)
from bianque.a2a.tasks import TaskReader, detail_url, event_message, report_text
from bianque.agents.events import PAYLOADS
from bianque.api.app import create_app
from bianque.auth.bearer import token_sha256
from bianque.config import Settings
from bianque.storage.models import A2ATask, InvestigationEvent

REPO = Path(__file__).resolve().parents[3]
CASES = REPO / "eval" / "cases"
HASH = token_sha256("token-a")


def entry(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {"name": "agent-a", "token_sha256": HASH, "namespaces": ["demo-s1"]}
    values.update(overrides)
    return values


# The Agent Card -------------------------------------------------------------------------


def test_the_card_announces_one_skill_one_interface_and_bearer_auth() -> None:
    card = build_card(Settings(a2a_public_url="https://bianque.example.com/a2a"))
    document = MessageToDict(card)
    for field in (
        "name",
        "description",
        "supportedInterfaces",
        "version",
        "capabilities",
        "defaultInputModes",
        "defaultOutputModes",
        "skills",
    ):
        assert document[field], field
    assert document["supportedInterfaces"] == [
        {
            "url": "https://bianque.example.com/a2a",
            "protocolBinding": "JSONRPC",
            "protocolVersion": "1.0",
        }
    ]
    assert document["capabilities"] == {
        "streaming": True,
        "pushNotifications": False,
        "extendedAgentCard": False,
    }
    assert document["securitySchemes"] == {
        "bearer": {
            "httpAuthSecurityScheme": {
                "scheme": "Bearer",
                "description": "A token issued to the client; each token reads its namespaces.",
            }
        }
    }
    assert document["securityRequirements"] == [{"schemes": {"bearer": {}}}]
    assert document["defaultInputModes"] == ["application/json"]
    [skill] = document["skills"]
    assert skill["id"] == SKILL_ID == "investigate_alert"
    assert skill["name"] and skill["description"] and skill["tags"]
    assert skill["inputModes"] == ["application/json"]


async def test_the_card_is_served_without_a_token() -> None:
    app = create_app(Settings(a2a_public_url="https://bianque.example.com/a2a"))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/.well-known/agent-card.json")
    assert response.status_code == 200 and response.headers["etag"]
    card = ParseDict(response.json(), AgentCard())
    assert card.supported_interfaces[0].url == "https://bianque.example.com/a2a"


# Configuration --------------------------------------------------------------------------


def test_clients_are_read_from_json_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BIANQUE_A2A_CLIENTS", json.dumps([entry()]))
    [client] = Settings().a2a_clients
    assert (client.name, client.token_sha256, client.namespaces) == ("agent-a", HASH, ("demo-s1",))
    monkeypatch.setenv("BIANQUE_A2A_CLIENTS", "")
    assert Settings().a2a_clients == ()
    monkeypatch.setenv("BIANQUE_A2A_CLIENTS", "{}")
    with pytest.raises(ValidationError, match="BIANQUE_A2A_CLIENTS must be a JSON array"):
        Settings()


@pytest.mark.parametrize(
    "overrides",
    [
        {"token_sha256": "not-a-hash"},
        {"token_sha256": HASH.upper()},
        {"namespaces": []},
        {"namespaces": ["Not_A_Namespace"]},
        {"name": "Has Space"},
        {"token": "plain"},
        {"tools": ["*"]},
    ],
)
def test_a_malformed_client_is_refused(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({"a2a_clients": [entry(**overrides)]})


def test_two_clients_cannot_share_a_name_or_a_token() -> None:
    other = token_sha256("token-b")
    with pytest.raises(ValidationError, match="two A2A clients share a name"):
        Settings.model_validate({"a2a_clients": [entry(), entry(token_sha256=other)]})
    with pytest.raises(ValidationError, match="two A2A clients share a token_sha256"):
        Settings.model_validate({"a2a_clients": [entry(), entry(name="agent-b")]})


@pytest.mark.parametrize("url", ["127.0.0.1:8080/a2a", "ftp://host/a2a", "http://", "http://h /a"])
def test_the_public_url_is_an_absolute_http_address(url: str) -> None:
    with pytest.raises(ValidationError):
        Settings(a2a_public_url=url)


# Intake ---------------------------------------------------------------------------------


def _alert(status: str = "firing", **labels: str) -> dict[str, Any]:
    body = json.loads((CASES / "s1-a" / "pack" / "alert.json").read_text())
    item: dict[str, Any] = body["alerts"][0]
    item["status"] = status
    item["labels"].update(labels)
    return item


def _part(value: Any) -> Part:
    return Part(data=ParseDict(value, struct_pb2.Value()))


PACK = Settings(tool_backend="pack", cases_dir=CASES)
LIVE = Settings(tool_backend="live", cases_dir=CASES)
NAMESPACES = frozenset({"demo-s1"})


def test_only_data_parts_are_read() -> None:
    assert data_contents([_part({"case_id": "s1-a"}), _part([1])]) == [{"case_id": "s1-a"}, [1]]
    for other in (Part(text="hello"), Part(url="https://example.com"), Part(raw=b"x")):
        with pytest.raises(ContentTypeNotSupportedError):
            data_contents([_part({"case_id": "s1-a"}), other])


def test_a_case_or_an_alert_is_accepted_where_it_fits() -> None:
    assert read_request([{"case_id": "s1-b"}], PACK, NAMESPACES) == CaseRequest("s1-b")
    accepted = read_request([_alert()], LIVE, NAMESPACES)
    assert isinstance(accepted, AlertRequest)
    assert accepted.alert.labels["namespace"] == "demo-s1"


@pytest.mark.parametrize(
    ("settings", "contents", "reason"),
    [
        (PACK, [], "malformed"),
        (PACK, [{"case_id": "s1-a"}, {"case_id": "s1-a"}], "malformed"),
        (PACK, ["s1-a"], "malformed"),
        (PACK, [{"name": "s1-a"}], "malformed"),
        (PACK, [{"case_id": "s1-a", "extra": 1}], "malformed"),
        (PACK, [{"case_id": None}], "malformed"),
        (PACK, [_alert()], "profile_mismatch"),
        (LIVE, [{"case_id": "s1-a"}], "profile_mismatch"),
        (PACK, [{"case_id": "nope"}], "unknown_case"),
        (PACK, [{"case_id": "S1-A"}], "unknown_case"),
        (LIVE, [{"labels": {}, "status": "firing"}], "malformed"),
        (LIVE, [_alert(namespace="other")], "namespace_denied"),
        (LIVE, [{**_alert(), "labels": {"alertname": "x"}}], "namespace_denied"),
        (LIVE, [_alert("resolved")], "resolved_alert"),
        (LIVE, [_alert("resolved", namespace="other")], "namespace_denied"),
    ],
)
def test_a_request_that_cannot_be_served_is_rejected(
    settings: Settings, contents: list[Any], reason: str
) -> None:
    rejected = read_request(contents, settings, NAMESPACES)
    assert isinstance(rejected, Rejected) and rejected.reason == reason
    assert rejected.detail


def test_a_case_outside_the_clients_namespaces_is_denied() -> None:
    rejected = read_request([{"case_id": "s1-a"}], PACK, frozenset({"other"}))
    assert rejected == Rejected("namespace_denied", REJECTIONS["namespace_denied"])


def test_a_profile_mismatch_says_what_the_server_takes() -> None:
    rejected = read_request([{"case_id": "s1-a"}], LIVE, NAMESPACES)
    assert isinstance(rejected, Rejected)
    assert rejected.detail == (
        'this Bianque takes an Alertmanager alert, not a {"case_id": "<case>"} request'
    )


# Texts of a task ------------------------------------------------------------------------

NOW = datetime(2026, 10, 21, 8, 0, tzinfo=UTC)
TASK = A2ATask(
    id="task_1", context_id="ctx_1", investigation_id="inv_1", caller="agent-a", created_at=NOW
)

#: One payload of every event type.
PAYLOAD_EXAMPLES: dict[str, dict[str, Any]] = {
    "investigation_started": {"trigger": "a2a"},
    "plan_created": {"plan_id": 3, "version": 2, "source": "model"},
    "step_started": {"step_id": 7, "domain": "kubernetes", "status": "running"},
    "step_finished": {"step_id": 7, "domain": "kubernetes", "status": "done"},
    "evidence_added": {"evidence_id": "ev_0123456789", "source": "kubernetes", "tool": "k8s_x"},
    "assessment_recorded": {"round": 1, "sufficient": False, "rule_override": False},
    "report_rejected": {"attempt": 1, "violations": ["citation_integrity", "schema"]},
    "report_published": {"report_id": 4},
    "report_degraded": {"report_id": 4},
    "investigation_finished": {"status": "reported"},
    "investigation_interrupted": {"status": "interrupted"},
}

EXPECTED_TEXTS = {
    "investigation_started": "investigation started (a2a)",
    "plan_created": "plan version 2 (model)",
    "step_started": "step 7 started (kubernetes)",
    "step_finished": "step 7 done (kubernetes)",
    "evidence_added": "evidence ev_0123456789 added (kubernetes, k8s_x)",
    "assessment_recorded": "round 1 assessment: insufficient",
    "report_rejected": "report attempt 1 rejected by citation_integrity, schema",
    "report_published": "report published",
    "report_degraded": "degraded report published",
    "investigation_finished": "investigation finished: reported",
    "investigation_interrupted": "investigation interrupted",
}


def test_every_event_type_has_a_status_text() -> None:
    assert set(PAYLOAD_EXAMPLES) == set(PAYLOADS) == set(EXPECTED_TEXTS)
    for seq, (event_type, payload) in enumerate(PAYLOAD_EXAMPLES.items(), start=1):
        event = InvestigationEvent(
            investigation_id="inv_1", seq=seq, type=event_type, payload=payload, created_at=NOW
        )
        message = event_message(TASK, event)
        text, data = message.parts
        assert text.text == EXPECTED_TEXTS[event_type]
        assert MessageToDict(data.data) == {"event": event_type, "seq": seq, **payload}
        assert (message.task_id, message.context_id) == ("task_1", "ctx_1")
        assert message.message_id == f"task_1:{seq}"


def test_a_rejected_task_says_why() -> None:
    row = A2ATask(
        id="task_2", context_id="ctx_1", caller="agent-a", rejected_reason="unknown_case",
        created_at=NOW,
    )  # fmt: skip
    task = TaskReader(None, "http://localhost/a2a")._rejected(row)  # type: ignore[arg-type]
    text, data = task.status.message.parts
    assert text.text == f"unknown_case: {REJECTIONS['unknown_case']}"
    assert MessageToDict(data.data) == {"reason": "unknown_case"}
    assert "investigation_id" not in task.metadata


def test_the_detail_address_shares_the_origin_of_the_endpoint() -> None:
    assert detail_url("http://127.0.0.1:8080/a2a", "inv_1") == (
        "http://127.0.0.1:8080/investigations/inv_1"
    )
    assert detail_url("https://bianque.example.com/x/a2a", "inv_1") == (
        "https://bianque.example.com/investigations/inv_1"
    )


def test_the_report_text_is_built_from_the_report() -> None:
    report = {
        "summary": "The pod restarts.",
        "root_cause": {
            "status": "identified",
            "category": "config_error",
            "confidence": "high",
            "statement": "A commit changed APP_MODE.",
        },
    }
    suggestions: list[dict[str, Any]] = [
        {
            "id": "sg_1",
            "action": "Revert the commit.",
            "command": "git revert abc",
            "risk": "change",
            "state": "pending",
        },
        {
            "id": "sg_2",
            "action": "Watch it.",
            "command": None,
            "risk": "read_only",
            "state": "confirmed",
        },
    ]
    assert report_text(report, "published", suggestions, "http://x/investigations/inv_1") == (
        "Report: published\n"
        "Summary: The pod restarts.\n"
        "Root cause (identified, category config_error, confidence high): "
        "A commit changed APP_MODE.\n"
        "Suggestions:\n"
        "1. [change, pending] Revert the commit. `git revert abc` (sg_1)\n"
        "2. [read_only, confirmed] Watch it. (sg_2)\n"
        "Details and confirmation: http://x/investigations/inv_1"
    )
    unknown = {"summary": "s", "root_cause": {"status": "unknown", "statement": "Unknown."}}
    assert report_text(unknown, "degraded", [], "u").splitlines()[2:] == [
        "Root cause (unknown): Unknown.",
        "Suggestions: none",
        "Details and confirmation: u",
    ]
