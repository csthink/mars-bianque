"""Server-sent events: frames, and the response that streams them.

A frame is ``id`` (when the stream can be resumed), ``event`` and one ``data`` line of
JSON. The response turns off caching and tells a buffering proxy not to buffer.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from typing import Any

from fastapi.responses import StreamingResponse

#: Headers of every event stream.
HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def frame(event: str, data: Mapping[str, Any], *, event_id: int | None = None) -> str:
    """Return one event frame."""
    lines = [] if event_id is None else [f"id: {event_id}"]
    lines += [f"event: {event}", f"data: {json.dumps(data, ensure_ascii=False)}"]
    return "\n".join(lines) + "\n\n"


def comment(text: str) -> str:
    """Return a comment line; clients ignore it, proxies see a live connection."""
    return f": {text}\n\n"


def stream(frames: AsyncIterator[str]) -> StreamingResponse:
    """Return a response that sends ``frames`` as they are produced."""
    return StreamingResponse(frames, media_type="text/event-stream", headers=HEADERS)
