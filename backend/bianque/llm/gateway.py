"""The single egress to the model service.

This is the only module in the repository allowed to import ``bianque.llm.providers``.
Every other module, inside the ``bianque.llm`` package or outside it, is forbidden to,
otherwise a second path to the model service exists. ``tests/test_llm_egress_static.py``
enforces this.

## Three gates

Every request passes three gates before it reaches the provider:

1. **Static type gate**: the methods accept only ``RedactedPrompt``, so mypy strict
   rejects a plain ``str`` at type-check time.
2. **Runtime gate**: an ``isinstance`` check raises ``TypeError`` for anything that is
   not a ``RedactedPrompt`` whose message contents and tool call arguments are
   ``RedactedText``. This covers dynamic construction and deserialization.
3. **Pre-send recheck**: the redaction engine's ``detect()`` runs on every message text
   and every tool call argument right before sending and raises on any residue. This
   covers a ``RedactedText`` forged with the construction sentinel.

## Around the provider call

- **Retries**: timeouts, connection failures, HTTP 429 and 5xx are retried up to
  ``BIANQUE_LLM_MAX_RETRIES`` times with growing delays; other failures are not.
- **Call records**: every call, successful or not, is reported to the configured
  ``CallRecorder`` with its context, summaries, retries, latency and token usage.
- **Structured output**: ``complete_structured`` declares the output schema as a function,
  forces the model to call it, validates the arguments with pydantic and gives the model
  one chance to correct invalid arguments.

## The public surface exposes only scalars

The gateway's public attributes hold no provider object, no HTTP or SDK client and no
transport. Handing out any of them would let a caller send a raw string past all three
gates. Observability needs are met by read-only scalars: ``provider_name`` and
``endpoint_host``. The regression test is
``tests/test_llm_redaction_gate.py::test_gateway_public_surface_exposes_no_network_client``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError

from bianque.config import Settings
from bianque.llm.errors import (
    LLMError,
    LLMUnavailableError,
    RetryableProviderError,
    StructuredOutputError,
)
from bianque.llm.providers import LLMProvider, ProviderOptions, build_provider
from bianque.llm.redacted import (
    RedactedPrompt,
    RedactedText,
    assistant_message,
    build_redaction_engine,
    tool_result_message,
)
from bianque.llm.request import ModelRequest
from bianque.llm.scripted import ScriptedRule
from bianque.llm.types import (
    CallContext,
    CallEvent,
    LLMCompletion,
    ToolCall,
    ToolChoice,
    ToolSpec,
)
from bianque.redaction import RedactionEngine

__all__ = [
    "CallRecorder",
    "LLMGateway",
    "RedactionResidueError",
    "StructuredResult",
    "build_gateway",
]

#: Delay in seconds before retry ``n`` (1-based) is ``RETRY_BASE_DELAY * 2 ** (n - 1)``.
RETRY_BASE_DELAY = 1.0


class RedactionResidueError(LLMError):
    """The pre-send recheck found sensitive residue; nothing was sent."""


class CallRecorder(Protocol):
    """Receives one event per model call."""

    async def record(self, event: CallEvent) -> None:
        """Store or forward the event."""
        ...


class _NoRecorder:
    async def record(self, event: CallEvent) -> None:
        return None


@dataclass(frozen=True)
class StructuredResult[T: BaseModel]:
    """A validated structured output and the completion that carried it."""

    value: T
    completion: LLMCompletion
    #: 1 when the first answer was valid, 2 when the correction was needed.
    attempts: int


def _summarize_prompt(prompt: RedactedPrompt) -> str:
    lines = []
    for message in prompt.messages:
        lines.append(f"{message.role}: {message.content.text}")
        lines.extend(
            f"{message.role} calls {call.name}: {call.arguments.text}"
            for call in message.tool_calls
        )
    return "\n".join(lines)


def _summarize_completion(completion: LLMCompletion) -> str:
    lines = [completion.text] if completion.text else []
    lines.extend(f"calls {call.name}: {call.arguments}" for call in completion.tool_calls)
    return "\n".join(lines)


def _first_call(calls: Sequence[ToolCall], name: str) -> tuple[ToolCall | None, str]:
    for call in calls:
        if call.name == name:
            return call, ""
    return None, f"the model did not call {name}"


def _describe_validation_error(error: ValidationError) -> str:
    parts = []
    for item in error.errors(include_url=False, include_input=False):
        location = ".".join(str(part) for part in item["loc"]) or "(root)"
        parts.append(f"{location}: {item['msg']}")
    return "; ".join(parts)


def function_parameters(output: type[BaseModel]) -> dict[str, Any]:
    """Return the JSON Schema of ``output`` as clean function parameters.

    pydantic puts the model's ``title`` and docstring ``description`` at the top level of
    the schema. Some models read a top-level ``description`` as a parameter and nest all
    arguments under it, so both keys are dropped; the function itself carries the
    description.
    """
    schema = output.model_json_schema()
    return {key: value for key, value in schema.items() if key not in ("title", "description")}


class LLMGateway:
    """The single egress to the model service."""

    def __init__(
        self,
        provider: LLMProvider,
        *,
        engine: RedactionEngine,
        model: str,
        max_retries: int = 2,
        extra_body: dict[str, Any] | None = None,
        recorder: CallRecorder | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Assemble the gateway.

        Args:
            provider: The provider for the configured model mode.
            engine: The redaction engine used for the pre-send recheck and for messages
                the gateway adds itself; it must match the engine used by callers.
            model: The configured model name, part of every request key.
            max_retries: Retries after retryable provider failures.
            extra_body: Provider-specific request body fields.
            recorder: Receives one event per call; nothing is recorded when omitted.
            sleep: Waits between retries; tests pass a function that does not wait.
        """
        self._provider = provider
        self._engine = engine
        self._model = model
        self._max_retries = max_retries
        self._extra_body = dict(extra_body or {})
        self._recorder: CallRecorder = recorder or _NoRecorder()
        self._sleep = sleep

    @property
    def provider_name(self) -> str:
        """The name of the provider implementation behind this gateway.

        Only this scalar is public, never the provider object: the provider holds the
        SDK client, and with it a caller could send requests past the gates.
        """
        return self._provider.name

    @property
    def endpoint_host(self) -> str | None:
        """The host requests are sent to; ``None`` when the provider has no network endpoint."""
        return self._provider.endpoint_host

    def _require_redacted(self, payload: Any) -> tuple[RedactedText, ...]:
        """Runtime gate: return every redacted text in ``payload`` or raise.

        Raises:
            TypeError: ``payload`` is not a ``RedactedPrompt``, or a message content or a
                tool call argument did not go through the redaction pipeline.
        """
        if not isinstance(payload, RedactedPrompt):
            raise TypeError(
                "the model gateway accepts only a RedactedPrompt built by "
                f"redact_prompt_for_llm(), got {type(payload)!r}"
            )
        texts: list[RedactedText] = []
        for index, message in enumerate(payload.messages):
            content: object = message.content
            if not isinstance(content, RedactedText):
                raise TypeError(
                    f"message {index} of the prompt did not go through the redaction "
                    f"pipeline: {type(content)!r}"
                )
            texts.append(content)
            for call in message.tool_calls:
                arguments: object = call.arguments
                if not isinstance(arguments, RedactedText):
                    raise TypeError(
                        f"a tool call of message {index} did not go through the redaction "
                        f"pipeline: {type(arguments)!r}"
                    )
                texts.append(arguments)
        return tuple(texts)

    def _recheck(self, texts: tuple[RedactedText, ...]) -> None:
        """Pre-send recheck: run ``detect()`` on every text and fail closed on residue.

        Raises:
            RedactionResidueError: A text still contains sensitive content.
        """
        for index, item in enumerate(texts):
            findings = self._engine.detect(item.text)
            if findings:
                rules = sorted({finding.rule for finding in findings})
                raise RedactionResidueError(
                    f"pre-send recheck found sensitive residue in text {index} "
                    f"(rules {rules!r}); the input bypassed redact_for_llm()"
                )

    async def complete(
        self,
        prompt: RedactedPrompt,
        *,
        context: CallContext,
        tools: Sequence[ToolSpec] = (),
        tool_choice: ToolChoice = "auto",
    ) -> LLMCompletion:
        """Make one model call after all three gates pass.

        Args:
            prompt: The redacted messages.
            context: Who makes the call; recorded with it.
            tools: Functions the model may call.
            tool_choice: ``auto``, ``required`` or the name of a function to force.

        Returns:
            The completion.

        Raises:
            TypeError: The prompt did not go through the redaction pipeline.
            RedactionResidueError: The pre-send recheck found sensitive content.
            LLMError: The call failed; the failure is recorded before it is raised.
        """
        self._recheck(self._require_redacted(prompt))
        request = ModelRequest(
            agent=context.agent,
            operation=context.operation,
            prompt=prompt,
            model=self._model,
            tools=tuple(tools),
            tool_choice=tool_choice,
            extra_body=self._extra_body,
        )
        started_at = datetime.now(UTC)
        clock = time.monotonic()
        retries = 0
        while True:
            try:
                completion = await self._provider.complete(request)
            except RetryableProviderError as exc:
                if retries < self._max_retries:
                    retries += 1
                    await self._sleep(RETRY_BASE_DELAY * 2 ** (retries - 1))
                    continue
                failure: Exception = LLMUnavailableError(exc.category, retries + 1)
                await self._record(context, prompt, started_at, clock, retries, error=failure)
                raise failure from exc
            except Exception as exc:
                await self._record(context, prompt, started_at, clock, retries, error=exc)
                raise
            await self._record(context, prompt, started_at, clock, retries, completion=completion)
            return completion

    async def _record(
        self,
        context: CallContext,
        prompt: RedactedPrompt,
        started_at: datetime,
        clock: float,
        retries: int,
        *,
        completion: LLMCompletion | None = None,
        error: BaseException | None = None,
    ) -> None:
        await self._recorder.record(
            CallEvent(
                agent=context.agent,
                name=context.operation,
                investigation_id=context.investigation_id,
                session_id=context.session_id,
                input_summary=_summarize_prompt(prompt),
                output_summary=_summarize_completion(completion) if completion else None,
                status="ok" if error is None else "error",
                error=f"{type(error).__name__}: {error}" if error is not None else None,
                retries=retries,
                started_at=started_at,
                latency_ms=round((time.monotonic() - clock) * 1000),
                tokens_in=completion.usage.prompt_tokens if completion else None,
                tokens_out=completion.usage.completion_tokens if completion else None,
            )
        )

    async def complete_structured[T: BaseModel](
        self,
        prompt: RedactedPrompt,
        *,
        context: CallContext,
        output: type[T],
        function_name: str,
        description: str,
    ) -> StructuredResult[T]:
        """Get output that validates against ``output`` by forcing a function call.

        The schema of ``output`` is declared as the only function and the model is forced
        to call it. When the arguments are missing, not JSON, or fail validation, the
        call and the validation errors are appended to the conversation and the model
        gets one more attempt.

        Args:
            prompt: The redacted messages.
            context: Who makes the call.
            output: The pydantic model of the expected output.
            function_name: Name of the declared function.
            description: Description of the declared function.

        Returns:
            The validated output, the accepted completion and the number of attempts.

        Raises:
            StructuredOutputError: The output was still invalid after the correction.
            LLMError: A model call failed.
        """
        tool = ToolSpec(function_name, description, function_parameters(output))
        conversation = prompt
        problem = ""
        for attempt in (1, 2):
            completion = await self.complete(
                conversation, context=context, tools=(tool,), tool_choice=function_name
            )
            call, problem = _first_call(completion.tool_calls, function_name)
            if call is not None:
                try:
                    value = output.model_validate_json(call.arguments)
                except ValidationError as exc:
                    problem = _describe_validation_error(exc)
                else:
                    return StructuredResult(value, completion, attempt)
            # Show the model its own call and why it was rejected, then ask again. When
            # it made no call, a placeholder call keeps the tool message well-formed.
            rejected = call or ToolCall(f"{function_name}-missing", function_name, "{}")
            conversation = conversation + (
                assistant_message(completion.text, (rejected,), engine=self._engine),
                tool_result_message(
                    rejected.id,
                    f"The arguments were rejected: {problem}. Call {function_name} again "
                    "with arguments that satisfy its schema.",
                    engine=self._engine,
                ),
            )
        raise StructuredOutputError(
            f"{context.operation}: output still invalid after one correction: {problem}"
        )


def build_gateway(
    settings: Settings,
    *,
    recorder: CallRecorder | None = None,
    scripted_rules: Sequence[ScriptedRule] = (),
    http_client: Any = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> LLMGateway:
    """Assemble the gateway for the configured model mode.

    Args:
        settings: Process settings.
        recorder: Receives one event per call.
        scripted_rules: Rules for ``scripted`` mode, tried before the fallback rule.
        http_client: Optional ``httpx.AsyncClient``. Tests inject one backed by
            ``MockTransport``; production code always passes ``None``.
        sleep: Waits between retries; tests pass a function that does not wait.

    Returns:
        The gateway.

    Raises:
        ValueError: ``settings.llm_mode`` has no registered provider.
    """
    return LLMGateway(
        build_provider(
            settings, ProviderOptions(http_client=http_client, scripted_rules=scripted_rules)
        ),
        engine=build_redaction_engine(settings),
        model=settings.llm_model,
        max_retries=settings.llm_max_retries,
        extra_body=settings.llm_extra_body,
        recorder=recorder,
        sleep=sleep,
    )
