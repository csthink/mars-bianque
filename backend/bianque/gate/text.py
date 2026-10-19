"""Walk the strings of an output together with their JSON paths."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any


def join(path: str, key: str | int) -> str:
    """Append an object key or a list index to a JSON path."""
    if isinstance(key, int):
        return f"{path}[{key}]"
    return f"{path}.{key}" if path else key


def iter_strings(data: Any, path: str = "") -> Iterator[tuple[str, str]]:
    """Yield ``(path, value)`` for every string in a JSON value, in document order."""
    if isinstance(data, str):
        yield path, data
    elif isinstance(data, dict):
        for key, value in data.items():
            yield from iter_strings(value, join(path, str(key)))
    elif isinstance(data, list):
        for index, value in enumerate(data):
            yield from iter_strings(value, join(path, index))


def top_key(path: str) -> str:
    """Return the top-level field of a JSON path: ``claims`` for ``claims[2].statement``."""
    return path.split(".", 1)[0].split("[", 1)[0]
