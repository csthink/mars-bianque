"""Alertmanager alerts: what an investigation keeps of an incoming alert.

An alert becomes the first evidence of its investigation. This module maps the fields
of one Alertmanager alert to that evidence: the text stored as evidence and the
identifiers redaction must keep. A new alert source adds its own mapping.

The module does not import FastAPI: the command line uses it as well as the webhook.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

#: Labels whose values name the objects an alert is about. Redaction keeps these values,
#: so that a model can pass them to the first tool calls.
IDENTIFIER_LABELS = (
    "alertname",
    "namespace",
    "pod",
    "container",
    "deployment",
    "statefulset",
    "daemonset",
    "job",
)


class AlertFormatError(ValueError):
    """The body is not an Alertmanager webhook with at least one alert."""


@dataclass(frozen=True)
class AlertInput:
    """One alert of an Alertmanager webhook.

    Attributes:
        fingerprint: The Alertmanager fingerprint.
        status: ``firing`` or ``resolved``.
        labels: The alert's labels.
        annotations: The alert's annotations.
        starts_at: When the alert started firing.
        payload: The alert object exactly as received.
    """

    fingerprint: str
    status: str
    labels: Mapping[str, str]
    annotations: Mapping[str, str]
    starts_at: datetime
    payload: Mapping[str, Any]

    @property
    def text(self) -> str:
        """The alert as evidence text: indented JSON with sorted keys."""
        return json.dumps(self.payload, indent=2, sort_keys=True, ensure_ascii=False)

    @property
    def identifiers(self) -> tuple[str, ...]:
        """The alert name and the names of the objects the alert is about, sorted."""
        values = {str(self.labels[name]) for name in IDENTIFIER_LABELS if self.labels.get(name)}
        return tuple(sorted(values))


def parse_alert(alert: Mapping[str, Any]) -> AlertInput:
    """Map one alert object of a webhook.

    Raises:
        AlertFormatError: A required field is missing or malformed.
    """
    try:
        return AlertInput(
            fingerprint=str(alert["fingerprint"]),
            status=str(alert["status"]),
            labels={str(k): str(v) for k, v in dict(alert.get("labels") or {}).items()},
            annotations={str(k): str(v) for k, v in dict(alert.get("annotations") or {}).items()},
            starts_at=datetime.fromisoformat(str(alert["startsAt"])),
            payload=dict(alert),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise AlertFormatError(f"malformed alert: {type(exc).__name__}: {exc}") from exc


def parse_webhook(body: Mapping[str, Any]) -> list[AlertInput]:
    """Return the alerts of an Alertmanager webhook body, in their order.

    Raises:
        AlertFormatError: The body has no alerts or an alert is malformed.
    """
    alerts = body.get("alerts")
    if not isinstance(alerts, list) or not alerts:
        raise AlertFormatError("the webhook body has no alerts")
    return [parse_alert(alert) for alert in alerts]
