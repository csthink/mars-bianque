"""What an A2A message asks for: an alert to investigate, a case to replay, or nothing.

A message starts a task with exactly one ``data`` part. A message with any other kind
of part (text, raw bytes, a URL) is answered ContentTypeNotSupported and starts no task:
Bianque takes no free-text conversation. Everything else starts a task, and a request
Bianque cannot serve ends that task at once as ``REJECTED`` with a reason code:

- ``malformed``: not exactly one data part, or its content is neither an alert nor a
  case request. A content with a ``case_id`` key is a case request; one with ``labels``
  or ``fingerprint`` is an alert, the fields of one alert of an Alertmanager webhook.
- ``profile_mismatch``: a case request where Bianque reads a cluster (the ``live``
  tool backend), or an alert where it replays evidence packs (``pack``).
- ``unknown_case``: no evidence pack for the case.
- ``namespace_denied``: the alert's ``namespace`` label, or that of the case's alert, is
  missing or not one of the client's namespaces. It is checked before the alert's
  status, so a client learns nothing about alerts outside its namespaces.
- ``resolved_alert``: the alert is not firing.

This module decides; it neither stores nor starts anything.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from a2a.types import ContentTypeNotSupportedError, Part
from google.protobuf.json_format import MessageToDict

from bianque.agents.service import CaseNotFoundError, case_alert
from bianque.api.alertmanager import AlertFormatError, AlertInput, parse_alert
from bianque.config import Settings

#: Each reason code of a rejected task, with the sentence its status message carries.
REJECTIONS: Mapping[str, str] = {
    "malformed": (
        "send one data part: a firing Alertmanager alert where Bianque reads a cluster, "
        'or {"case_id": "<case>"} where it replays evidence packs'
    ),
    "namespace_denied": "the alert's namespace is not one this client may read",
    "profile_mismatch": "this Bianque takes {expected}, not {given}",
    "unknown_case": "no case of that ID has an evidence pack",
    "resolved_alert": "only a firing alert starts an investigation",
}

#: What each tool backend takes.
_TAKES = {"live": "an Alertmanager alert", "pack": 'a {"case_id": "<case>"} request'}


@dataclass(frozen=True)
class CaseRequest:
    """Replay a case: start a new investigation of its evidence pack."""

    case_id: str


@dataclass(frozen=True)
class AlertRequest:
    """Investigate an alert, or follow the investigation it is already under."""

    alert: AlertInput


@dataclass(frozen=True)
class Rejected:
    """The task ends as ``REJECTED`` for ``reason``.

    Attributes:
        reason: A reason code of ``REJECTIONS``.
        detail: The sentence of the status message.
    """

    reason: str
    detail: str


Request = CaseRequest | AlertRequest | Rejected


def data_contents(parts: Sequence[Part]) -> list[Any]:
    """Return the content of each data part of a message, in order.

    Raises:
        ContentTypeNotSupportedError: A part is not a data part.
    """
    contents = []
    for part in parts:
        if part.WhichOneof("content") != "data":
            raise ContentTypeNotSupportedError(
                message="Bianque takes one data part (application/json) and no other content"
            )
        contents.append(MessageToDict(part.data))
    return contents


def rejected(reason: str, *, detail: str | None = None) -> Rejected:
    """Return the rejection for ``reason`` with its usual sentence."""
    return Rejected(reason, detail if detail is not None else REJECTIONS[reason])


def read_request(
    contents: Sequence[Any], settings: Settings, namespaces: Collection[str]
) -> Request:
    """Decide what the data parts of a message ask for.

    Args:
        contents: The content of each data part (``data_contents``).
        settings: Process settings: the tool backend and the cases directory.
        namespaces: The namespaces the client may read.
    """
    if len(contents) != 1 or not isinstance(contents[0], dict):
        return rejected("malformed")
    content: dict[str, Any] = contents[0]
    if "case_id" in content:
        given = "case"
    elif "labels" in content or "fingerprint" in content:
        given = "live"
    else:
        return rejected("malformed")
    if (given == "case") != (settings.tool_backend == "pack"):
        detail = REJECTIONS["profile_mismatch"].format(
            expected=_TAKES[settings.tool_backend],
            given=_TAKES["pack" if given == "case" else "live"],
        )
        return rejected("profile_mismatch", detail=detail)
    if given == "case":
        return _case(content, settings, namespaces)
    return _alert(content, namespaces)


def _case(content: dict[str, Any], settings: Settings, namespaces: Collection[str]) -> Request:
    case_id = content["case_id"]
    if set(content) != {"case_id"} or not isinstance(case_id, str):
        return rejected("malformed")
    try:
        alert = case_alert(settings, case_id)
    except CaseNotFoundError:
        return rejected("unknown_case")
    if alert.labels.get("namespace") not in namespaces:
        return rejected("namespace_denied")
    return CaseRequest(case_id)


def _alert(content: dict[str, Any], namespaces: Collection[str]) -> Request:
    try:
        alert = parse_alert(content)
    except AlertFormatError:
        return rejected("malformed")
    if alert.labels.get("namespace") not in namespaces:
        return rejected("namespace_denied")
    if alert.status != "firing":
        return rejected("resolved_alert")
    return AlertRequest(alert)
