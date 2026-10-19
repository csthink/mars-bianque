"""Value types shared by the model gateway, its callers and the providers.

These types live outside the provider layer so that callers can use them without
importing ``bianque.llm.providers``, which only the gateway may import.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

#: How the model may use the tools of a request: ``auto`` lets it choose, ``required``
#: makes it call one of them, and a tool name makes it call that tool.
ToolChoice = str

#: The tool choice values that are not tool names.
TOOL_CHOICE_MODES = ("auto", "required")

#: Characters per token used when a provider reports no usage. Roughly right for
#: English text; it only has to be deterministic.
CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """Estimate the token count of ``text`` as characters divided by 4, rounded up."""
    return math.ceil(len(text) / CHARS_PER_TOKEN)


@dataclass(frozen=True)
class ToolSpec:
    """A function the model may call: name, description and JSON Schema of the arguments."""

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    """One function call returned by the model; ``arguments`` is the raw JSON text."""

    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class CallContext:
    """Who makes a model call and for what.

    ``agent`` is part of the request key used for recorded responses; the
    investigation and session IDs differ on every run, so they are not.
    """

    #: ``investigation``, ``kubernetes``, ``logs``, ``change`` or ``followup``.
    agent: str
    #: The call site, such as ``plan`` or ``report``; stored as the call record name.
    operation: str
    investigation_id: str | None = None
    session_id: int | None = None


@dataclass(frozen=True)
class LLMUsage:
    """Token usage of one model call.

    ``estimated`` is true when the provider reported no usage and the counts were
    estimated from text length.
    """

    prompt_tokens: int
    completion_tokens: int
    estimated: bool = False


@dataclass(frozen=True)
class LLMCompletion:
    """The result of one model call."""

    text: str
    model: str
    usage: LLMUsage
    tool_calls: tuple[ToolCall, ...] = ()
    #: ``stop``, ``tool_calls``, ``length`` or another value reported by the provider.
    finish_reason: str = "stop"


@dataclass(frozen=True)
class CallEvent:
    """What the gateway reports about one model call, success or failure."""

    agent: str
    name: str
    investigation_id: str | None
    session_id: int | None
    input_summary: str
    output_summary: str | None
    status: Literal["ok", "error"]
    error: str | None
    retries: int
    started_at: datetime
    latency_ms: int
    tokens_in: int | None
    tokens_out: int | None
    kind: Literal["model"] = "model"


@dataclass
class MemoryCallRecorder:
    """A call recorder that keeps events in memory, for tests and local checks."""

    events: list[CallEvent] = field(default_factory=list)

    async def record(self, event: CallEvent) -> None:
        """Keep the event."""
        self.events.append(event)
