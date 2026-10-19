"""Rule-based responder used in ``scripted`` mode.

``scripted`` mode answers model calls without any model: it runs the rules in order,
and the first rule that returns a reply wins. It drives unit tests, failure paths and
the end-to-end tests of the replay image, which is why it lives in the product package
rather than in test code. It is only active when ``BIANQUE_LLM_MODE=scripted`` is set.

The built-in fallback rule makes any call answerable: when the request forces a
function, it returns the smallest arguments that satisfy the function's JSON Schema;
otherwise it returns empty text and calls no tool. Agent roles register their own
rules (read the evidence and tools in the request, decide which tool to call and which
evidence to cite) when those roles are built; tests inject extra rules the same way.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from bianque.llm.request import ModelRequest
from bianque.llm.types import TOOL_CHOICE_MODES, ToolCall


@dataclass(frozen=True)
class ScriptedReply:
    """What a rule answers: text and tool calls (arguments as Python values)."""

    text: str = ""
    tool_calls: tuple[tuple[str, dict[str, Any]], ...] = ()


#: A rule returns a reply for requests it handles and ``None`` for the rest.
ScriptedRule = Callable[[ModelRequest], ScriptedReply | None]


def minimal_instance(schema: dict[str, Any], definitions: dict[str, Any] | None = None) -> Any:
    """Return the smallest value that satisfies a JSON Schema.

    Covers the subset that pydantic emits: objects with required properties, arrays
    with ``minItems``, strings with ``minLength``, ``enum`` or a ``date-time``, ``date``
    or ``uuid`` format, numbers with inclusive or exclusive bounds, booleans, ``null``,
    ``const``, ``$ref`` into ``$defs``, and ``anyOf``/``oneOf`` (first branch). Regular
    expression ``pattern`` and ``multipleOf`` are not covered; a role whose output needs
    them registers its own rule.

    Args:
        schema: The schema.
        definitions: ``$defs`` of the enclosing schema; taken from ``schema`` when omitted.

    Returns:
        A JSON-compatible value.
    """
    defs = definitions if definitions is not None else schema.get("$defs", {})
    if "$ref" in schema:
        return minimal_instance(defs[str(schema["$ref"]).rsplit("/", 1)[-1]], defs)
    if "const" in schema:
        return schema["const"]
    if "enum" in schema:
        return schema["enum"][0]
    for combinator in ("anyOf", "oneOf", "allOf"):
        if combinator in schema:
            return minimal_instance(schema[combinator][0], defs)
    kind = schema.get("type", "object")
    if isinstance(kind, list):
        kind = kind[0]
    if kind == "object":
        properties = schema.get("properties", {})
        return {
            name: minimal_instance(properties[name], defs) for name in schema.get("required", [])
        }
    if kind == "array":
        return [minimal_instance(schema.get("items", {}), defs)] * int(schema.get("minItems", 0))
    if kind == "string":
        if schema.get("format") in _FORMAT_EXAMPLES:
            return _FORMAT_EXAMPLES[schema["format"]]
        return "x" * int(schema.get("minLength", 0))
    if kind in ("integer", "number"):
        return _minimal_number(schema, integer=kind == "integer")
    if kind == "boolean":
        return False
    return None


#: Fixed values for string formats that pydantic emits for dates and UUIDs.
_FORMAT_EXAMPLES = {
    "date-time": "1970-01-01T00:00:00Z",
    "date": "1970-01-01",
    "uuid": "00000000-0000-0000-0000-000000000000",
}


def _minimal_number(schema: dict[str, Any], *, integer: bool) -> int | float:
    """Return the number closest to zero within the schema's bounds."""
    low = schema.get("minimum")
    high = schema.get("maximum")
    if "exclusiveMinimum" in schema:
        bound = schema["exclusiveMinimum"]
        low = bound + 1 if integer else (bound + (high if high is not None else bound + 2)) / 2
    if "exclusiveMaximum" in schema:
        bound = schema["exclusiveMaximum"]
        high = bound - 1 if integer else (bound + (low if low is not None else bound - 2)) / 2
    value: int | float = 0
    if low is not None and value < low:
        value = low
    if high is not None and value > high:
        value = high
    return int(value) if integer else value


def forced_function_rule(request: ModelRequest) -> ScriptedReply | None:
    """Fallback rule: answer a forced function call with minimal valid arguments."""
    if request.tool_choice in TOOL_CHOICE_MODES and request.tool_choice != "required":
        return None
    candidates = [
        tool
        for tool in request.tools
        if request.tool_choice == "required" or tool.name == request.tool_choice
    ]
    if not candidates:
        return None
    tool = candidates[0]
    return ScriptedReply(tool_calls=((tool.name, minimal_instance(tool.parameters)),))


@dataclass
class ScriptedResponder:
    """Runs rules in order; injected rules come before the built-in fallback."""

    rules: Sequence[ScriptedRule] = field(default_factory=tuple)

    def reply(self, request: ModelRequest) -> ScriptedReply:
        """Return the reply of the first rule that handles ``request``."""
        for rule in (*self.rules, forced_function_rule):
            reply = rule(request)
            if reply is not None:
                return reply
        return ScriptedReply()


def to_tool_calls(reply: ScriptedReply) -> tuple[ToolCall, ...]:
    """Turn a reply's tool calls into ``ToolCall`` values with stable IDs."""
    return tuple(
        ToolCall(
            id=f"scripted-{index}",
            name=name,
            arguments=json.dumps(arguments, sort_keys=True, ensure_ascii=False),
        )
        for index, (name, arguments) in enumerate(reply.tool_calls)
    )
