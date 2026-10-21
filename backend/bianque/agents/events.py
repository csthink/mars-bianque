"""Progress events of investigations: their payloads, and the in-process notification.

Every event type has one payload model. A payload holds IDs, statuses,
counters and validator names; each string field is either an enumeration or a pattern
of an identifier, so no field can carry model text. ``EventWriter`` validates every
payload against its model before the event is stored; a payload that does not fit is a
defect of the code that emitted it.

``EventNotifier`` wakes the readers of an investigation's event stream after a new event
is committed (``docs/adr/0004-postgresql-only.md``, decision 2). The database stays the
only source of events: a reader that is woken, or that waited long enough, reads the new
rows from the table.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from bianque.agents.domains import DOMAINS
from bianque.gate import CHECKLISTS

#: The names a violation list may hold: the validators, and ``checklist`` for an
#: output type without a checklist.
VALIDATOR_NAMES = sorted(
    {"schema", "checklist", *(v.__name__ for checks in CHECKLISTS.values() for v in checks)}
)


def _choice(values: list[str]) -> str:
    return "^(" + "|".join(values) + ")$"


DomainName = Annotated[str, Field(pattern=_choice([item.name for item in DOMAINS]))]
ValidatorName = Annotated[str, Field(pattern=_choice(VALIDATOR_NAMES))]
#: Tool names of the registry are lowercase words joined by underscores.
ToolName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")]
EvidenceId = Annotated[str, Field(pattern=r"^ev_[0-9a-f]{10}$")]
StepStatus = Literal["pending", "running", "done", "failed"]
InvestigationStatus = Literal["pending", "running", "reported", "degraded", "interrupted", "failed"]


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class InvestigationStarted(_Payload):
    """The investigation started."""

    trigger: Literal["alert", "manual", "replay", "a2a"]


class PlanCreated(_Payload):
    """A plan version was stored."""

    plan_id: int
    version: int
    source: Literal["model", "default"]


class StepChanged(_Payload):
    """A step started or finished."""

    step_id: int
    domain: DomainName
    status: StepStatus


class EvidenceAdded(_Payload):
    """New evidence was stored."""

    evidence_id: EvidenceId
    source: Literal["alertmanager", "kubernetes", "git"]
    tool: ToolName | None


class AssessmentRecorded(_Payload):
    """The assessment of a round was stored."""

    round: int
    sufficient: bool
    rule_override: bool


class ReportRejected(_Payload):
    """The evidence gate rejected a report attempt."""

    attempt: int
    violations: list[ValidatorName]


class ReportStored(_Payload):
    """The published or the degraded report was stored."""

    report_id: int


class InvestigationEnded(_Payload):
    """The investigation finished or was interrupted."""

    status: InvestigationStatus


#: The payload model of each event type.
PAYLOADS: Mapping[str, type[_Payload]] = {
    "investigation_started": InvestigationStarted,
    "plan_created": PlanCreated,
    "step_started": StepChanged,
    "step_finished": StepChanged,
    "evidence_added": EvidenceAdded,
    "assessment_recorded": AssessmentRecorded,
    "report_rejected": ReportRejected,
    "report_published": ReportStored,
    "report_degraded": ReportStored,
    "investigation_finished": InvestigationEnded,
    "investigation_interrupted": InvestigationEnded,
}

#: After these events an investigation has no further events.
TERMINAL_EVENTS = frozenset({"investigation_finished", "investigation_interrupted"})


def event_payload(event_type: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a payload against its event type and return it as JSON values.

    Raises:
        KeyError: The event type is not declared.
        pydantic.ValidationError: The payload does not fit the event type.
    """
    return PAYLOADS[event_type].model_validate(dict(payload)).model_dump(mode="json")


class EventNotifier:
    """Wakes the readers of an investigation's events in this process."""

    def __init__(self) -> None:
        """Start with no readers."""
        self._waiters: dict[str, set[asyncio.Event]] = {}

    def notify(self, investigation_id: str) -> None:
        """Wake every reader of ``investigation_id``."""
        for waiter in self._waiters.get(investigation_id, ()):
            waiter.set()

    async def wait(self, investigation_id: str, timeout: float) -> None:
        """Return when an event of ``investigation_id`` is committed, or after ``timeout``.

        A notification that arrives between two waits is not kept, so readers read the
        table again after every wait instead of relying on the wake-up alone.
        """
        waiter = asyncio.Event()
        waiters = self._waiters.setdefault(investigation_id, set())
        waiters.add(waiter)
        try:
            await asyncio.wait_for(waiter.wait(), timeout)
        except TimeoutError:
            pass
        finally:
            waiters.discard(waiter)
            if not waiters:
                self._waiters.pop(investigation_id, None)
