"""Recorded model responses: the file format, writing and lookup by request key.

A recording file holds one normalized request and the response the model gave to it.
Files are written by ``live`` mode when a record directory is configured, committed
under ``eval/recordings/<case>/``, and read by ``recorded`` mode, one case directory at a
time (``Settings.for_case``).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bianque.config.manifest import MANIFEST_FILE
from bianque.llm.errors import RecordingFormatError
from bianque.llm.request import ModelRequest, normalize_request, request_key
from bianque.llm.types import LLMCompletion, LLMUsage, ToolCall

#: Version of the recording file format; files with another version fail to load.
SCHEMA_VERSION = 1

#: Number of request key characters in a recording file name.
_FILE_KEY_LENGTH = 16


@dataclass(frozen=True)
class Recording:
    """One recorded request and its response."""

    key: str
    agent: str
    operation: str
    completion: LLMCompletion


def _completion_to_json(completion: LLMCompletion) -> dict[str, Any]:
    return {
        "text": completion.text,
        "model": completion.model,
        "finish_reason": completion.finish_reason,
        "tool_calls": [
            {"id": call.id, "name": call.name, "arguments": call.arguments}
            for call in completion.tool_calls
        ],
        "usage": {
            "prompt_tokens": completion.usage.prompt_tokens,
            "completion_tokens": completion.usage.completion_tokens,
            "estimated": completion.usage.estimated,
        },
    }


def _completion_from_json(data: dict[str, Any]) -> LLMCompletion:
    usage = data["usage"]
    return LLMCompletion(
        text=str(data["text"]),
        model=str(data["model"]),
        finish_reason=str(data["finish_reason"]),
        tool_calls=tuple(
            ToolCall(id=str(call["id"]), name=str(call["name"]), arguments=str(call["arguments"]))
            for call in data["tool_calls"]
        ),
        usage=LLMUsage(
            prompt_tokens=int(usage["prompt_tokens"]),
            completion_tokens=int(usage["completion_tokens"]),
            estimated=bool(usage["estimated"]),
        ),
    )


def write_recording(directory: Path, request: ModelRequest, completion: LLMCompletion) -> Path:
    """Write the recording of one call into ``directory``.

    Args:
        directory: Target directory, created when missing.
        request: The request that was sent.
        completion: The response that came back.

    Returns:
        The path of the written file, ``<agent>-<first 16 key characters>.json``.
    """
    key = request_key(request)
    document = {
        "schema_version": SCHEMA_VERSION,
        "key": key,
        "agent": request.agent,
        "operation": request.operation,
        "model": request.model,
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "request": normalize_request(request),
        "response": _completion_to_json(completion),
    }
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{request.agent}-{key[:_FILE_KEY_LENGTH]}.json"
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return path


def load_recording(path: Path) -> Recording:
    """Load one recording file.

    Raises:
        RecordingFormatError: The schema version differs or a field is missing.
    """
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        version = document.get("schema_version")
        if version != SCHEMA_VERSION:
            raise RecordingFormatError(
                f"{path}: recording schema version {version!r}, expected {SCHEMA_VERSION}"
            )
        return Recording(
            key=str(document["key"]),
            agent=str(document["agent"]),
            operation=str(document["operation"]),
            completion=_completion_from_json(document["response"]),
        )
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise RecordingFormatError(f"{path}: invalid recording file: {exc}") from exc


def iter_recording_files(directory: Path) -> Iterator[Path]:
    """Yield the recording files of one directory in a stable order.

    Subdirectories are not searched: they hold other cases. The recording manifest is
    not a recording.
    """
    if directory.is_dir():
        yield from sorted(p for p in directory.glob("*.json") if p.name != MANIFEST_FILE)


def index_recordings(directory: Path) -> dict[str, Recording]:
    """Load the recordings of one directory, keyed by request key.

    Raises:
        RecordingFormatError: A file is invalid, or two files record the same key.
    """
    index: dict[str, Recording] = {}
    origin: dict[str, Path] = {}
    for path in iter_recording_files(directory):
        recording = load_recording(path)
        if recording.key in index:
            raise RecordingFormatError(
                f"request key {recording.key} is recorded twice: {origin[recording.key]} and {path}"
            )
        index[recording.key] = recording
        origin[recording.key] = path
    return index
