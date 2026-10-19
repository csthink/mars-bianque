"""Redaction types for the model gateway: the static type gate and the only constructor.

The model gateway accepts only input that went through the redaction pipeline. This
module provides the types that carry that constraint:

- ``RedactedText``: text that went through the redaction pipeline. Callers cannot
  construct it directly: ``__init__`` requires a module-private sentinel held only by
  ``redact_for_llm()``. A plain ``str`` therefore cannot pass as gateway input at type
  check time (mypy strict), and there is no ordinary runtime path that builds one.
- ``RedactedPrompt`` and ``RedactedMessage``: a chat message sequence whose contents are
  always ``RedactedText``, validated element by element at construction time against
  dynamic construction and deserialization. Tool call arguments in assistant messages
  are ``RedactedText`` as well: they are sent back to the model with the next request.
- ``redact_for_llm()``: the only factory. It rewrites text with the ``bianque.redaction``
  engine and attaches the sentinel.

Rewrite rules, structure preservation, idempotence and ``detect()`` live in
``bianque.redaction``; this module only enforces the types and assembles the engine.
The internal domain suffixes come from ``bianque.config``
(``BIANQUE_REDACT_INTERNAL_DOMAINS``); this module does not read the process environment.

This module does not import the provider layer; only ``bianque.llm.gateway`` does.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from bianque.config import Settings, get_settings
from bianque.llm.types import ToolCall
from bianque.redaction import RedactionEngine, build_engine

#: Construction sentinel: the only valid credential for building a ``RedactedText``,
#: held by ``redact_for_llm()``. It is not exported from the package; a value forged
#: with it is caught by the gateway's ``detect()`` recheck.
_CONSTRUCTION_SENTINEL: Final = object()

#: Chat roles of the OpenAI-compatible protocol.
MessageRole = Literal["system", "user", "assistant", "tool"]


@dataclass(frozen=True, init=False)
class RedactedText:
    """Text that went through the redaction pipeline; built only by ``redact_for_llm()``.

    Frozen so that the text cannot be swapped back to the original after redaction.
    """

    text: str

    def __init__(self, text: str, *, sentinel: object = None) -> None:
        """Check the construction sentinel, then store the text.

        Args:
            text: Text already rewritten by the redaction engine.
            sentinel: Construction sentinel. Must be this module's
                ``_CONSTRUCTION_SENTINEL``.

        Raises:
            TypeError: The sentinel is missing, i.e. the text did not come from
                ``redact_for_llm()``.
        """
        if sentinel is not _CONSTRUCTION_SENTINEL:
            raise TypeError(
                "RedactedText can only be built by bianque.llm.redacted.redact_for_llm()"
            )
        object.__setattr__(self, "text", text)


@dataclass(frozen=True)
class RedactedToolCall:
    """A tool call made by the model, as it is sent back in a later request."""

    id: str
    name: str
    arguments: RedactedText


@dataclass(frozen=True)
class RedactedMessage:
    """One redacted chat message.

    An assistant message may carry the tool calls the model made; a ``tool`` message
    carries the result of one call and names it in ``tool_call_id``.
    """

    role: MessageRole
    content: RedactedText
    tool_calls: tuple[RedactedToolCall, ...] = ()
    tool_call_id: str | None = None


@dataclass(frozen=True, init=False)
class RedactedPrompt:
    """A redacted chat message sequence; every element is a ``RedactedMessage``."""

    messages: tuple[RedactedMessage, ...]

    def __init__(self, messages: Iterable[RedactedMessage]) -> None:
        """Validate each element at runtime, then freeze the sequence as a tuple.

        Type annotations reject wrong elements at type-check time; the runtime check
        covers dynamic construction and deserialization, such as
        ``RedactedPrompt(json.loads(...))``.

        Args:
            messages: The message sequence.

        Raises:
            TypeError: An element is not a ``RedactedMessage``, or its content is not a
                ``RedactedText``.
        """
        items = tuple(messages)
        for index, message in enumerate(items):
            if not isinstance(message, RedactedMessage):
                raise TypeError(
                    f"element {index} of RedactedPrompt is not a RedactedMessage: {type(message)!r}"
                )
            if not isinstance(message.content, RedactedText):
                raise TypeError(
                    f"content of message {index} of RedactedPrompt did not go through "
                    f"the redaction pipeline: {type(message.content)!r}"
                )
            for call in message.tool_calls:
                if not isinstance(call, RedactedToolCall) or not isinstance(
                    call.arguments, RedactedText
                ):
                    raise TypeError(
                        f"a tool call of message {index} of RedactedPrompt did not go "
                        "through the redaction pipeline"
                    )
        object.__setattr__(self, "messages", items)

    def __add__(self, other: Iterable[RedactedMessage]) -> RedactedPrompt:
        """Return a new prompt with ``other`` appended."""
        return RedactedPrompt((*self.messages, *other))


def parse_internal_domain_suffixes(raw: str) -> tuple[str, ...]:
    """Parse the comma-separated internal domain suffix setting.

    Args:
        raw: The value of ``BIANQUE_REDACT_INTERNAL_DOMAINS``, such as
            ``"internal.example,corp.example"``.

    Returns:
        The suffixes with whitespace and empty items removed; an empty tuple when the
        setting is empty (the engine then has no internal domain rule).
    """
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def build_redaction_engine(settings: Settings) -> RedactionEngine:
    """Build the redaction engine with the configured internal domain suffixes.

    Args:
        settings: Process settings.

    Returns:
        An engine carrying the internal domain suffixes.
    """
    return build_engine(parse_internal_domain_suffixes(settings.redact_internal_domains))


def redact_for_llm(text: str, *, engine: RedactionEngine | None = None) -> RedactedText:
    """Run text through the redaction pipeline; the only way to build ``RedactedText``.

    Args:
        text: Raw text (logs, events, configuration, diffs, user messages).
        engine: The redaction engine; when omitted, one is built from the current
            settings via ``get_settings()``.

    Returns:
        The redacted text.
    """
    active = engine if engine is not None else build_redaction_engine(get_settings())
    return RedactedText(active.redact(text), sentinel=_CONSTRUCTION_SENTINEL)


def redact_prompt_for_llm(
    messages: Iterable[tuple[MessageRole, str]],
    *,
    engine: RedactionEngine | None = None,
) -> RedactedPrompt:
    """Run a sequence of ``(role, raw text)`` pairs through the redaction pipeline.

    Args:
        messages: ``(role, raw text)`` pairs.
        engine: The redaction engine; when omitted, one is built as in
            ``redact_for_llm()``.

    Returns:
        A chat message sequence whose contents are all redacted.
    """
    active = engine if engine is not None else build_redaction_engine(get_settings())
    return RedactedPrompt(
        RedactedMessage(role, redact_for_llm(raw, engine=active)) for role, raw in messages
    )


def redact_tool_call(
    call_id: str, name: str, arguments: str, *, engine: RedactionEngine | None = None
) -> RedactedToolCall:
    """Run the arguments of a model tool call through the redaction pipeline.

    Args:
        call_id: The call ID the model gave.
        name: The function name.
        arguments: The raw JSON arguments.
        engine: The redaction engine; built as in ``redact_for_llm()`` when omitted.

    Returns:
        The tool call with redacted arguments.
    """
    return RedactedToolCall(call_id, name, redact_for_llm(arguments, engine=engine))


def tool_result_message(
    call_id: str, result: str, *, engine: RedactionEngine | None = None
) -> RedactedMessage:
    """Build the ``tool`` message that returns a result for the call ``call_id``.

    Args:
        call_id: The ID of the tool call being answered.
        result: The raw result text.
        engine: The redaction engine; built as in ``redact_for_llm()`` when omitted.

    Returns:
        The redacted tool message.
    """
    return RedactedMessage("tool", redact_for_llm(result, engine=engine), tool_call_id=call_id)


def assistant_message(
    text: str, tool_calls: Sequence[ToolCall] = (), *, engine: RedactionEngine | None = None
) -> RedactedMessage:
    """Build the assistant message that records a model reply in the conversation.

    Args:
        text: The text the model returned.
        tool_calls: The tool calls the model made.
        engine: The redaction engine; built as in ``redact_for_llm()`` when omitted.

    Returns:
        The redacted assistant message, ready to be sent back with the next request.
    """
    active = engine if engine is not None else build_redaction_engine(get_settings())
    return RedactedMessage(
        "assistant",
        redact_for_llm(text, engine=active),
        tool_calls=tuple(
            redact_tool_call(call.id, call.name, call.arguments, engine=active)
            for call in tool_calls
        ),
    )
