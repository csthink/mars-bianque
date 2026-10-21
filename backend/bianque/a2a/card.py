"""The Agent Card: what Bianque offers A2A clients, served at the well-known path.

The card needs no token. It announces one skill, investigating a Kubernetes alert, and
one interface: the JSON-RPC binding at ``BIANQUE_A2A_PUBLIC_URL``, which takes a bearer
token. Responses stream; there are no push notifications and no extended card.
"""

from __future__ import annotations

from importlib.metadata import version

from a2a.server.routes import create_agent_card_routes
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    HTTPAuthSecurityScheme,
    SecurityRequirement,
    SecurityScheme,
    StringList,
)
from a2a.utils.constants import (
    AGENT_CARD_WELL_KNOWN_PATH,
    PROTOCOL_VERSION_CURRENT,
    TransportProtocol,
)
from starlette.routing import BaseRoute

from bianque.config import Settings

#: The one skill.
SKILL_ID = "investigate_alert"

#: The name of the bearer scheme in the card's security schemes.
SECURITY_SCHEME = "bearer"

#: A task is started by one JSON object: an alert, or a case to replay.
INPUT_MODES = ("application/json",)

#: The report comes back as JSON and as a plain text summary.
OUTPUT_MODES = ("application/json", "text/plain")

_DESCRIPTION = (
    "Alert-driven root cause analysis for Kubernetes. Bianque plans an investigation, "
    "collects evidence with read-only tools and returns a report whose every conclusion "
    "cites the evidence it rests on."
)

_SKILL_DESCRIPTION = (
    "Analyse the root cause of one Kubernetes alert and return a report that cites its "
    "evidence. Send one data part: a firing Alertmanager alert, as one element of a "
    'webhook\'s "alerts" list, where Bianque reads a cluster; or {"case_id": "<case>"} '
    "where it replays recorded evidence packs. The alert's namespace must be one the "
    "token may read. Progress arrives as task status updates; the report arrives as the "
    "artifact named report. Remediation suggestions are confirmed by a person on "
    "Bianque's pages, never through A2A."
)


def build_card(settings: Settings) -> AgentCard:
    """Return the Agent Card; its interface address is ``BIANQUE_A2A_PUBLIC_URL``."""
    return AgentCard(
        name="Bianque",
        description=_DESCRIPTION,
        version=version("bianque"),
        supported_interfaces=[
            AgentInterface(
                url=settings.a2a_public_url,
                protocol_binding=TransportProtocol.JSONRPC.value,
                protocol_version=PROTOCOL_VERSION_CURRENT,
            )
        ],
        capabilities=AgentCapabilities(
            streaming=True, push_notifications=False, extended_agent_card=False
        ),
        security_schemes={
            SECURITY_SCHEME: SecurityScheme(
                http_auth_security_scheme=HTTPAuthSecurityScheme(
                    scheme="Bearer",
                    description="A token issued to the client; each token reads its namespaces.",
                )
            )
        },
        security_requirements=[SecurityRequirement(schemes={SECURITY_SCHEME: StringList()})],
        default_input_modes=list(INPUT_MODES),
        default_output_modes=list(OUTPUT_MODES),
        skills=[
            AgentSkill(
                id=SKILL_ID,
                name="Investigate an alert",
                description=_SKILL_DESCRIPTION,
                tags=["kubernetes", "root-cause-analysis", "alertmanager"],
                examples=[
                    '{"case_id": "s1-a"}',
                    '{"status": "firing", "labels": {"alertname": "KubePodCrashLooping", '
                    '"namespace": "demo-s1", "pod": "s1-app-7d9c"}, "annotations": {}, '
                    '"startsAt": "2026-10-21T08:00:00Z", "fingerprint": "3f2a9c01de4b7a65"}',
                ],
                input_modes=list(INPUT_MODES),
                output_modes=list(OUTPUT_MODES),
            )
        ],
    )


def card_routes(settings: Settings) -> list[BaseRoute]:
    """Return the route that serves the Agent Card at the well-known path."""
    return list(create_agent_card_routes(build_card(settings), card_url=AGENT_CARD_WELL_KNOWN_PATH))
