"""Provider for ``recorded`` mode: answers are recorded ``live`` responses.

The recordings directory is indexed on the first call, not at construction, so that
building a gateway never reads files. A request whose key has no recording fails; there
is no fallback to another mode, so stale recordings fail loudly instead of silently
answering with something else.
"""

from __future__ import annotations

from pathlib import Path

from bianque.llm.errors import RecordingNotFoundError
from bianque.llm.recording import Recording, index_recordings
from bianque.llm.request import ModelRequest, request_key
from bianque.llm.types import LLMCompletion


class RecordedProvider:
    """Provider that answers from recording files under one directory."""

    def __init__(self, root: Path) -> None:
        """Remember the recordings directory.

        Args:
            root: The directory of one case's recording files.
        """
        self._root = root
        self._index: dict[str, Recording] | None = None

    @property
    def name(self) -> str:
        """The name of this implementation."""
        return "recorded"

    @property
    def endpoint_host(self) -> str | None:
        """Always ``None``: recorded mode has no network endpoint."""
        return None

    async def complete(self, request: ModelRequest) -> LLMCompletion:
        """Return the recorded response for the request key.

        Raises:
            RecordingNotFoundError: No recording has this request's key.
        """
        if self._index is None:
            self._index = index_recordings(self._root)
        key = request_key(request)
        recording = self._index.get(key)
        if recording is None:
            raise RecordingNotFoundError(
                f"no recorded response for request key {key} (agent {request.agent!r}, "
                f"operation {request.operation!r}) in {self._root}; the prompt, tool "
                "schemas or model changed since recording, so record again in live mode "
                "(recorded mode replays the case of an investigation; one without a case "
                "has no recordings)"
            )
        return recording.completion

    def __repr__(self) -> str:
        """Show the recordings directory."""
        return f"RecordedProvider(root={str(self._root)!r})"
