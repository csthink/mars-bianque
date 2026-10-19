"""The degraded report: what deterministic code can say when no model report passed.

A degraded report has the schema of every report. Its root cause is unknown, it has no
suggestions, and its claims are an evidence summary plus rule hints: facts read from the
structured fields of pod status and event evidence, and the root cause category such a
fact usually points to. Values are taken from parsed fields only, never cut out of the
evidence text, so a redaction placeholder is never split.

The hint rules are domain knowledge: a new domain changes them.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from bianque.gate.context import FailedCall
from bianque.gate.schemas import (
    SCHEMA_VERSION,
    Claim,
    MissingEvidence,
    Report,
    RootCause,
    RootCauseCategory,
    TimelineEntry,
)


@dataclass(frozen=True)
class EvidenceItem:
    """One piece of evidence of the investigation, as stored.

    Attributes:
        id: The evidence ID.
        source: ``alertmanager``, ``kubernetes`` or ``git``.
        tool: The tool that collected it; ``None`` for the alert.
        collected_at: When it was collected.
        redacted_content: The redacted text.
    """

    id: str
    source: str
    tool: str | None
    collected_at: datetime
    redacted_content: str


#: Container termination reasons and the category they point to.
_TERMINATION_HINTS: dict[str, RootCauseCategory] = {"OOMKilled": "resource_limit"}

#: Container waiting reasons and the category they point to.
_WAITING_HINTS: dict[str, RootCauseCategory] = {
    "ImagePullBackOff": "image_error",
    "ErrImagePull": "image_error",
    "InvalidImageName": "image_error",
    "CreateContainerConfigError": "config_error",
}

#: The message prefix of an ``Unhealthy`` event that points to a probe problem. A failed
#: liveness probe restarts the container; a failed readiness probe is not a hint, since
#: every crashing container fails it ("connection refused").
_LIVENESS_FAILURE = "Liveness probe failed"


@dataclass(frozen=True)
class _Fact:
    statement: str
    category: RootCauseCategory | None
    time: str | None = None


def _parse(item: EvidenceItem) -> dict[str, Any] | None:
    try:
        data = json.loads(item.redacted_content)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _container_facts(pod: Any, container: dict[str, Any], key: str) -> Iterator[_Fact]:
    state = container.get(key)
    if not isinstance(state, dict):
        return
    name = container.get("name")
    reason = state.get("reason")
    if state.get("state") == "terminated" and key == "last_state":
        yield _Fact(
            f"Container {name} of pod {pod} last terminated with reason {reason} "
            f"and exit code {state.get('exit_code')}.",
            _TERMINATION_HINTS.get(str(reason)),
            state.get("finished_at"),
        )
    elif state.get("state") == "waiting" and reason:
        yield _Fact(
            f"Container {name} of pod {pod} is waiting with reason {reason}.",
            _WAITING_HINTS.get(str(reason)),
        )


def _facts(item: EvidenceItem) -> Iterator[_Fact]:
    data = _parse(item)
    if data is None:
        return
    if item.tool == "k8s_get_pod_status":
        for container in data.get("containers") or []:
            for key in ("last_state", "state"):
                yield from _container_facts(data.get("name"), container, key)
    elif item.tool == "k8s_list_pods":
        for pod in data.get("pods") or []:
            for container in pod.get("containers") or []:
                yield from _container_facts(pod.get("name"), container, "state")
    elif item.tool == "k8s_list_events":
        for event in data.get("events") or []:
            message = str(event.get("message") or "")
            if event.get("reason") == "Unhealthy" and message.startswith(_LIVENESS_FAILURE):
                target = event.get("object") or {}
                yield _Fact(
                    f"Event Unhealthy on {target.get('kind')} {target.get('name')}: "
                    "the liveness probe failed.",
                    "probe_misconfig",
                )


def _summary_claim(item: EvidenceItem) -> Claim:
    origin = item.tool or "the alert webhook"
    return Claim(
        statement=f"Evidence from {origin} ({item.source}), collected at "
        f"{item.collected_at.isoformat()}.",
        citations=[item.id],
    )


def build_degraded_report(
    evidence: Sequence[EvidenceItem],
    failed_calls: Sequence[FailedCall],
    rejected_by: Sequence[str],
) -> Report:
    """Build the degraded report of an investigation whose report retries are used up.

    Args:
        evidence: All evidence of the investigation.
        failed_calls: The failed evidence tool calls of the investigation.
        rejected_by: The validator names that rejected the last model report.

    Returns:
        A report with an unknown root cause, an evidence summary, rule hints, the
        timeline of container terminations and the missing evidence. It passes the
        report checklist of the gate.
    """
    ordered = sorted(evidence, key=lambda item: item.id)
    facts: dict[str, list[str]] = {}
    hints: dict[RootCauseCategory, list[str]] = {}
    timeline: dict[tuple[str, str], list[str]] = {}
    for item in ordered:
        for fact in _facts(item):
            facts.setdefault(fact.statement, []).append(item.id)
            if fact.category is not None:
                hints.setdefault(fact.category, []).append(item.id)
            if fact.time is not None:
                timeline.setdefault((fact.time, fact.statement), []).append(item.id)

    claims = [_summary_claim(item) for item in ordered]
    claims += [
        Claim(statement=statement, citations=sorted(set(ids))) for statement, ids in facts.items()
    ]
    claims += [
        Claim(
            statement="Rule hint: the cited container state or event usually points to "
            f"the root cause category {category}.",
            citations=sorted(set(ids)),
        )
        for category, ids in sorted(hints.items())
    ]
    names = ", ".join(sorted(set(rejected_by))) or "no validator recorded"
    missing = {
        (call.tool, call.error): MissingEvidence(
            what=f"Result of {call.tool} ({call.domain} domain).",
            reason=f"The tool call failed: {call.error}.",
        )
        for call in failed_calls
    }
    return Report(
        schema_version=SCHEMA_VERSION,
        summary="Degraded report: the evidence gate rejected the model's report "
        f"({names}) and the retries are used up. It lists the collected evidence and "
        "deterministic rule hints.",
        impact="Not assessed: a degraded report states no conclusion about the impact.",
        timeline=[
            TimelineEntry(
                time=datetime.fromisoformat(time), event=event, citations=sorted(set(ids))
            )
            for (time, event), ids in sorted(timeline.items())
        ],
        root_cause=RootCause(
            status="unknown",
            statement="Unknown: no model report passed the evidence gate.",
            citations=[],
            confidence="low",
        ),
        claims=claims,
        missing_evidence=[missing[key] for key in sorted(missing)],
        suggestions=[],
        references=[],
        followup_questions=[],
    )
