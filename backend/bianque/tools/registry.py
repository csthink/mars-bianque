"""The tool registry: the one list of tools that agents and MCP clients can call.

A tool is an async function declared with ``@tool(...)``. The declaration states that
the tool is read-only, its kind, its evidence domain and evidence source (evidence
tools), its argument model, its timeout and the arguments that identify what it reads.
Registration happens when the tool module is imported, and a declaration that breaks a
rule raises ``ToolRegistrationError``, so a process with a bad tool never starts.

Two kinds of tools exist. An evidence tool belongs to one evidence domain; its result,
when called during an investigation, is stored as evidence. An auxiliary tool belongs to
no domain and is offered to MCP clients only; its result is never evidence.

The registry does not check domain names: evidence domains are declared once in
``bianque.agents.domains``, and this package must not depend on the agents.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel

from bianque.llm.types import ToolSpec
from bianque.tools.errors import ToolRegistrationError
from bianque.tools.output import ToolOutput

if TYPE_CHECKING:
    from bianque.tools.backends.base import ToolBackends

#: ``evidence`` tools collect evidence for an investigation; ``auxiliary`` tools serve
#: MCP clients only.
ToolKind = Literal["evidence", "auxiliary"]

#: The evidence table's source values a tool can produce.
EvidenceSource = Literal["kubernetes", "git"]

ToolFunction = Callable[[Any, "ToolBackends"], Awaitable[ToolOutput]]

#: Tool names: lowercase letters, digits and underscores, at most 64 characters, which
#: model function calling and MCP clients both accept.
NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


@dataclass(frozen=True)
class ToolDefinition:
    """A registered tool."""

    name: str
    description: str
    kind: ToolKind
    domain: str | None
    source: EvidenceSource | None
    read_only: bool
    timeout_s: float
    identity_args: tuple[str, ...]
    args_model: type[BaseModel]
    function: ToolFunction

    def spec(self) -> ToolSpec:
        """Return the function calling declaration of this tool."""
        schema = self.args_model.model_json_schema()
        schema.pop("title", None)
        return ToolSpec(self.name, self.description, schema)

    def identity(self, args: BaseModel) -> dict[str, Any]:
        """Return the identity arguments of a call, which key evidence and replay lookup."""
        values = args.model_dump()
        return {name: values[name] for name in self.identity_args}


class ToolRegistry:
    """Registered tools by name."""

    def __init__(self) -> None:
        """Start with no tools."""
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, definition: ToolDefinition) -> None:
        """Add a tool after checking the registry rules.

        Raises:
            ToolRegistrationError: The name is malformed or taken, the tool is not
                read-only, its kind and domain do not match, an identity argument is not
                an argument, or the timeout is not positive.
        """
        name = definition.name
        if not NAME_PATTERN.fullmatch(name):
            raise ToolRegistrationError(f"tool name {name!r} does not match {NAME_PATTERN.pattern}")
        if definition.read_only is not True:
            raise ToolRegistrationError(f"tool {name} is not declared read-only")
        if definition.kind == "evidence":
            if not definition.domain or definition.source is None:
                raise ToolRegistrationError(f"evidence tool {name} needs a domain and a source")
        elif definition.domain is not None or definition.source is not None:
            raise ToolRegistrationError(f"auxiliary tool {name} must not have a domain or source")
        fields = definition.args_model.model_fields
        unknown = [arg for arg in definition.identity_args if arg not in fields]
        if unknown:
            raise ToolRegistrationError(f"tool {name}: identity arguments {unknown} are not fields")
        if definition.timeout_s <= 0:
            raise ToolRegistrationError(f"tool {name}: timeout must be positive")
        if name in self._tools:
            raise ToolRegistrationError(f"tool {name} is already registered")
        self._tools[name] = definition

    def get(self, name: str) -> ToolDefinition | None:
        """Return the tool called ``name``, or ``None``."""
        return self._tools.get(name)

    def __iter__(self) -> Iterator[ToolDefinition]:
        """Iterate over the tools in name order."""
        return iter(sorted(self._tools.values(), key=lambda item: item.name))

    def specs(self, *, domain: str | None = None) -> list[ToolSpec]:
        """Return function calling declarations, optionally only one domain's evidence tools."""
        return [
            item.spec()
            for item in self
            if domain is None or (item.kind == "evidence" and item.domain == domain)
        ]

    def tool(
        self,
        *,
        name: str,
        kind: ToolKind,
        read_only: bool,
        timeout_s: float,
        args: type[BaseModel],
        domain: str | None = None,
        source: EvidenceSource | None = None,
        identity_args: tuple[str, ...] = (),
    ) -> Callable[[ToolFunction], ToolFunction]:
        """Declare a tool; the decorated function's docstring is its description."""

        def decorate(function: ToolFunction) -> ToolFunction:
            description = " ".join((function.__doc__ or "").split("\n\n")[0].split())
            self.register(
                ToolDefinition(
                    name=name,
                    description=description,
                    kind=kind,
                    domain=domain,
                    source=source,
                    read_only=read_only,
                    timeout_s=timeout_s,
                    identity_args=identity_args,
                    args_model=args,
                    function=function,
                )
            )
            return function

        return decorate


#: The process-wide registry. The tool modules register into it when imported.
registry = ToolRegistry()

#: Decorator that registers into the process-wide registry.
tool = registry.tool
