"""What a tool function returns, and how results are rendered.

A tool returns the text of its result and the identifiers found in it. The text is
JSON with a fixed field order, so the same data always renders to the same text: the
evidence ID and the request key of recorded model responses are both derived from it.
Times are absolute UTC timestamps, never "five minutes ago".
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolOutput:
    """The raw result of one tool call.

    Attributes:
        text: The result before redaction.
        identifiers: Object names, container names, container images, label values and
            commit hashes in the result. Redaction keeps them intact so that a later
            tool call can use them; they are stored with the evidence.
    """

    text: str
    identifiers: tuple[str, ...] = ()


def render(data: Any) -> str:
    """Render a result as indented JSON, keeping the field order of ``data``."""
    return json.dumps(data, indent=2, ensure_ascii=False)


def collect_identifiers(*groups: Iterable[str | None]) -> tuple[str, ...]:
    """Merge identifier groups into a sorted tuple without empty values or duplicates."""
    return tuple(sorted({item for group in groups for item in group if item}))
