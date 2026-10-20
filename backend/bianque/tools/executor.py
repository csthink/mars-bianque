"""The tool executor: the only way to call a tool.

Evidence sub-agents and the MCP service both call ``ToolExecutor.call``. Each call goes
through the same steps:

1. Look up the tool, check that the caller may use it, validate the arguments, check
   the namespace they name against the caller's.
2. Run it under its timeout. Timeouts, connection errors, HTTP 5xx and 429 are retried
   twice, after 0.5 and 1 second; other failures are final.
3. Redact the result. Identifiers in the structured result (object names, commit
   hashes) are preserved, so the model can pass them to the next tool.
4. During an investigation, store a successful evidence tool result as evidence, with
   an ID derived from the tool, its identity arguments and the redacted text. Calls
   without an investigation (MCP) and auxiliary tools never produce evidence.
5. Record the call, successful or not.
6. Return a ``ToolResult`` whose ``model_view`` is the only text that may reach a model.

Error texts carry the category and the object only, never timings or retry counts, so
that a failure reads the same on every run. PostgreSQL text cannot store NUL characters:
the executor escapes them in a caller's tool name and arguments, in tool output and in
error texts, so that every call can be recorded; argument values with one are invalid.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from bianque.config import get_settings
from bianque.llm.redacted import RedactedText, build_redaction_engine, redact_for_llm
from bianque.redaction import RedactionEngine
from bianque.storage.ids import evidence_id
from bianque.tools.backends.base import ToolBackends
from bianque.tools.errors import ToolError
from bianque.tools.output import ToolOutput
from bianque.tools.registry import ToolDefinition, ToolRegistry, registry
from bianque.tools.store import EvidenceRecord, ToolCallRecord, ToolCallSink

#: Delays in seconds before each retry; their count is the number of retries.
RETRY_DELAYS: tuple[float, ...] = (0.5, 1.0)

#: The longest tool name kept of a call to a tool that does not exist.
NAME_LIMIT = 128

ToolStatus = Literal["ok", "error", "rejected"]


@dataclass(frozen=True)
class CallerScope:
    """Who calls a tool and what they may call.

    Attributes:
        agent: Written to the call record: ``kubernetes``, ``logs``, ``change``, ``mcp``.
        domain: For an evidence sub-agent: only evidence tools of this domain.
        tools: For an MCP client: the tool names it may call.
        namespaces: For an MCP client: the namespaces its calls may name.
        caller: For an MCP client: its name, written to the call record.
    """

    agent: str
    domain: str | None = None
    tools: frozenset[str] | None = None
    namespaces: frozenset[str] | None = None
    caller: str | None = None


@dataclass(frozen=True)
class InvestigationContext:
    """The investigation (and step) a call collects evidence for."""

    investigation_id: str
    step_id: int | None = None


@dataclass(frozen=True)
class ToolResult:
    """The outcome of one tool call.

    Attributes:
        status: ``ok``, ``error`` (the tool failed) or ``rejected`` (not executed).
        evidence_ids: Evidence stored by this call; empty outside investigations.
        model_view: The redacted result or error text; the only text for the model.
        error: The error text (``<category>: <object>``) when not ``ok``.
        identifiers: The identifiers preserved by the redaction of this result.
    """

    status: ToolStatus
    evidence_ids: tuple[str, ...]
    model_view: RedactedText
    error: str | None = None
    identifiers: tuple[str, ...] = ()


def _describe(definition: ToolDefinition, args: BaseModel) -> str:
    values = definition.identity(args)
    parts = [f"{key}={value}" for key, value in values.items() if value is not None]
    return " ".join([definition.name, *parts])


def storable(text: str) -> str:
    """Return ``text`` with NUL characters escaped; PostgreSQL text cannot hold them."""
    return text.replace("\x00", "\\x00")


def _strings(arguments: Mapping[str, Any] | str) -> list[str]:
    # The string values a caller passed, as far as they can be read before validation.
    try:
        payload = json.loads(arguments) if isinstance(arguments, str) else dict(arguments)
    except ValueError:
        return []
    if not isinstance(payload, dict):
        return []
    return [value for value in payload.values() if isinstance(value, str)]


def _validation_summary(error: ValidationError) -> str:
    return storable(
        "; ".join(
            f"{'.'.join(str(p) for p in item['loc']) or 'arguments'}: {item['msg']}"
            for item in error.errors()
        )
    )


class ToolExecutor:
    """Runs tools for agents and MCP clients."""

    def __init__(
        self,
        backends: ToolBackends,
        sink: ToolCallSink,
        *,
        tools: ToolRegistry = registry,
        engine: RedactionEngine | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        retry_delays: Sequence[float] = RETRY_DELAYS,
    ) -> None:
        """Bind the executor to backends, a sink and a registry.

        Args:
            backends: Where the tools read from.
            sink: Where call records and evidence go.
            tools: The registry to look tools up in.
            engine: The redaction engine; built from the settings when omitted.
            sleep: Waits between retries; tests pass a function that does not wait.
            retry_delays: Delay before each retry.
        """
        self._backends = backends
        self._sink = sink
        self._tools = tools
        self._engine = engine if engine is not None else build_redaction_engine(get_settings())
        self._sleep = sleep
        self._delays = tuple(retry_delays)

    def _redact(self, text: str, preserve: Collection[str]) -> RedactedText:
        return redact_for_llm(text, engine=self._engine, preserve=preserve)

    async def call(
        self,
        name: str,
        arguments: Mapping[str, Any] | str,
        *,
        scope: CallerScope,
        investigation: InvestigationContext | None = None,
        preserve: Collection[str] = (),
    ) -> ToolResult:
        """Call a tool and return its redacted result.

        Args:
            name: The tool name.
            arguments: The arguments as a mapping or as JSON text from the model.
            scope: Who calls and what they may call.
            investigation: The investigation to store evidence for; ``None`` for MCP.
            preserve: Identifiers from earlier results that redaction must keep.

        Returns:
            The result; failures and rejections are results too, never exceptions.
        """
        started_at = datetime.now(UTC)
        clock = time.monotonic()
        raw_input = storable(
            arguments if isinstance(arguments, str) else json.dumps(dict(arguments))
        )
        definition = self._tools.get(name)
        name = storable(name)[:NAME_LIMIT]

        async def finish(
            status: ToolStatus,
            error: ToolError,
            *,
            input_text: str = raw_input,
            retries: int = 0,
            keep: Collection[str] = (),
        ) -> ToolResult:
            # The error text names the object the call was about; keep its name intact.
            keep = {*preserve, *keep}
            text = storable(error.model_text)
            view = self._redact(text, keep)
            detail = f" ({self._engine.redact(storable(error.detail))})" if error.detail else ""
            await self._sink.save(
                ToolCallRecord(
                    agent=scope.agent,
                    name=name,
                    caller=scope.caller,
                    investigation_id=investigation.investigation_id if investigation else None,
                    input_summary=self._redact(input_text, keep).text,
                    output_summary=view.text,
                    status=status,
                    error=text + detail,
                    retries=retries,
                    started_at=started_at,
                    latency_ms=round((time.monotonic() - clock) * 1000),
                ),
                None,
            )
            return ToolResult(status, (), view, text)

        if definition is None:
            return await finish("rejected", ToolError("unknown_tool", name))
        # The tool itself first: a caller learns nothing of the arguments of a tool it
        # may not call. The names it passed stay readable in the record.
        denial = self._check_tool(definition, scope)
        if denial is not None:
            return await finish("rejected", denial, keep=_strings(arguments))
        try:
            payload = json.loads(arguments) if isinstance(arguments, str) else dict(arguments)
            args = definition.args_model.model_validate(payload)
        except ValidationError as exc:
            return await finish(
                "rejected", ToolError("invalid_arguments", name, _validation_summary(exc))
            )
        except ValueError as exc:
            return await finish(
                "rejected", ToolError("invalid_arguments", name, storable(str(exc)))
            )
        input_text = json.dumps(args.model_dump())
        named = [str(value) for value in args.model_dump().values() if isinstance(value, str)]
        denial = self._check_namespace(args, scope)
        if denial is not None:
            return await finish("rejected", denial, input_text=input_text, keep=named)

        output, retries, failure = await self._run(definition, args)
        if failure is not None:
            return await finish(
                "error", failure, input_text=input_text, retries=retries, keep=named
            )
        assert output is not None

        identifiers = tuple(sorted({*output.identifiers, *preserve}))
        raw = storable(output.text)
        view = self._redact(raw, identifiers)
        evidence: EvidenceRecord | None = None
        if investigation is not None and definition.kind == "evidence":
            assert definition.source is not None
            evidence = EvidenceRecord(
                investigation_id=investigation.investigation_id,
                evidence_id=evidence_id(name, definition.identity(args), view.text),
                source=definition.source,
                tool=name,
                collected_at=datetime.now(UTC),
                raw_content=raw,
                redacted_content=view.text,
                identifiers=identifiers,
                step_id=investigation.step_id,
                meta={"arguments": args.model_dump(), "backend": self._backends.kind},
            )
        await self._sink.save(
            ToolCallRecord(
                agent=scope.agent,
                name=name,
                caller=scope.caller,
                investigation_id=investigation.investigation_id if investigation else None,
                input_summary=self._redact(input_text, identifiers).text,
                output_summary=view.text,
                status="ok",
                retries=retries,
                started_at=started_at,
                latency_ms=round((time.monotonic() - clock) * 1000),
            ),
            evidence,
        )
        evidence_ids = (evidence.evidence_id,) if evidence is not None else ()
        return ToolResult("ok", evidence_ids, view, None, identifiers)

    def _check_tool(self, definition: ToolDefinition, scope: CallerScope) -> ToolError | None:
        if scope.domain is not None and (
            definition.kind != "evidence" or definition.domain != scope.domain
        ):
            return ToolError(
                "rejected", f"{definition.name} is not a tool of the {scope.domain} domain"
            )
        if scope.tools is not None and definition.name not in scope.tools:
            return ToolError("rejected", f"{definition.name} is not allowed for this client")
        return None

    def _check_namespace(self, args: BaseModel, scope: CallerScope) -> ToolError | None:
        namespace = getattr(args, "namespace", None)
        if scope.namespaces is not None and namespace is not None:
            if namespace not in scope.namespaces:
                return ToolError(
                    "rejected", f"namespace {namespace} is not allowed for this client"
                )
        return None

    async def _run(
        self, definition: ToolDefinition, args: BaseModel
    ) -> tuple[ToolOutput | None, int, ToolError | None]:
        retries = 0
        while True:
            try:
                fault = self._backends.injected_fault(definition.name)
                if fault is not None:
                    raise fault
                async with asyncio.timeout(definition.timeout_s):
                    return await definition.function(args, self._backends), retries, None
            except TimeoutError:
                error = ToolError("timeout", _describe(definition, args))
            except ToolError as exc:
                error = exc
                if exc.category == "timeout":
                    error = ToolError("timeout", _describe(definition, args), exc.detail)
            except Exception as exc:
                error = ToolError(
                    "failed", _describe(definition, args), f"{type(exc).__name__}: {exc}"
                )
            if not error.retryable or retries >= len(self._delays):
                return None, retries, error
            await self._sleep(self._delays[retries])
            retries += 1
