"""Argument field types shared by the tools, and helpers for reading API objects.

Names follow the Kubernetes rules (RFC 1123 labels and subdomains). Validating them
before a backend is called keeps malformed input out of API paths and git arguments.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

Namespace = Annotated[
    str,
    Field(max_length=63, pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", description="Namespace."),
]
ObjectName = Annotated[
    str,
    Field(max_length=253, pattern=r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$"),
]
ContainerName = Annotated[
    str,
    Field(max_length=63, pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", description="Container."),
]
#: Equality-based label selector: ``key=value``, ``key==value`` or ``key!=value`` terms
#: separated by commas. Set-based selectors are not accepted, so the replay backend
#: only implements the semantics the real API applies to these terms.
LabelSelector = Annotated[
    str,
    Field(
        max_length=256,
        pattern=r"^[A-Za-z0-9._/-]+(==|=|!=)[A-Za-z0-9._-]*(,[A-Za-z0-9._/-]+(==|=|!=)[A-Za-z0-9._-]*)*$",
        description="Equality-based label selector, such as app=web,tier!=cache.",
    ),
]


class ToolArgs(BaseModel):
    """Base of tool argument models: unknown arguments are rejected."""

    model_config = ConfigDict(extra="forbid", frozen=True)


def parse_time(value: Any) -> datetime | None:
    """Parse an API timestamp; ``None`` for a missing or malformed value."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def format_time(value: Any) -> str | None:
    """Render an API timestamp as UTC to the second, ``2026-10-20T06:20:01Z``."""
    parsed = parse_time(value) if not isinstance(value, datetime) else value
    if parsed is None:
        return None
    return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def metadata(obj: dict[str, Any]) -> dict[str, Any]:
    """Return an object's metadata, or an empty dict."""
    value = obj.get("metadata")
    return value if isinstance(value, dict) else {}


def owner(obj: dict[str, Any]) -> dict[str, Any] | None:
    """Return the controlling owner as ``{kind, name}``, or ``None``.

    Kind and name stay separate fields: joined as ``Kind/name`` they would form one
    long token that the entropy fallback of the redaction could mask.
    """
    for ref in metadata(obj).get("ownerReferences") or []:
        if ref.get("controller"):
            return {"kind": ref.get("kind"), "name": ref.get("name")}
    return None


def status_words(entries: Iterable[Any], *keys: str) -> list[str]:
    """Collect the values of ``keys`` (such as ``type`` and ``reason``) from status entries.

    Condition types and reasons are API vocabulary (``PodReadyToStartContainers``,
    ``MinimumReplicasAvailable``). They are preserved through redaction like object
    names: long CamelCase words would otherwise look random to the entropy fallback.
    """
    return [
        str(entry[key])
        for entry in entries
        if isinstance(entry, dict)
        for key in keys
        if entry.get(key)
    ]
