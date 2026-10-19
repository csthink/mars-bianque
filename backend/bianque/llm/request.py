"""The request the gateway hands to a provider, and its request key.

The request key identifies a request for recorded responses: the SHA-256 of the agent
name, the normalized messages, the tool schemas, the sampling parameters and the model
name. For a replay to find its recordings, everything that differs between runs must
stay out of the key: investigation and session IDs are not part of the request, and
prompt builders keep times and run-specific IDs out of message text.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from bianque.llm.redacted import RedactedMessage, RedactedPrompt
from bianque.llm.types import ToolChoice, ToolSpec

#: Sampling temperature of every model call; fixed so that runs are comparable.
TEMPERATURE = 0.0


@dataclass(frozen=True)
class ModelRequest:
    """Everything a provider needs for one call, and everything the request key covers."""

    agent: str
    operation: str
    prompt: RedactedPrompt
    model: str
    tools: tuple[ToolSpec, ...] = ()
    tool_choice: ToolChoice = "auto"
    temperature: float = TEMPERATURE
    #: Provider-specific request body fields from ``BIANQUE_LLM_EXTRA_BODY``.
    extra_body: dict[str, Any] = field(default_factory=dict)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _normalize_arguments(arguments: str) -> str:
    """Re-serialize JSON arguments with sorted keys; keep invalid JSON as it is."""
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError:
        return arguments
    return _canonical_json(parsed)


def _normalize_message(message: RedactedMessage) -> dict[str, Any]:
    normalized: dict[str, Any] = {"role": message.role, "content": message.content.text}
    if message.tool_calls:
        normalized["tool_calls"] = [
            {
                "id": call.id,
                "name": call.name,
                "arguments": _normalize_arguments(call.arguments.text),
            }
            for call in message.tool_calls
        ]
    if message.tool_call_id is not None:
        normalized["tool_call_id"] = message.tool_call_id
    return normalized


def normalize_request(request: ModelRequest) -> dict[str, Any]:
    """Return the canonical form of a request, as hashed and as stored in recordings."""
    tools = sorted(
        (
            {"name": tool.name, "description": tool.description, "parameters": tool.parameters}
            for tool in request.tools
        ),
        key=lambda tool: str(tool["name"]),
    )
    return {
        "agent": request.agent,
        "model": request.model,
        "messages": [_normalize_message(message) for message in request.prompt.messages],
        "tools": tools,
        "sampling": {
            "temperature": request.temperature,
            "tool_choice": request.tool_choice,
            "extra_body": request.extra_body,
        },
    }


def request_key(request: ModelRequest) -> str:
    """Return the SHA-256 hex digest of the normalized request."""
    return hashlib.sha256(_canonical_json(normalize_request(request)).encode("utf-8")).hexdigest()
