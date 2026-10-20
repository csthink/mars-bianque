"""The recording manifest: what the committed recordings were recorded with.

``eval/recordings/manifest.json`` sits beside the case directories of recorded
responses. It is written by the recording run (``make record``) once every case has been
recorded and replayed. The request key covers the model name and the provider-specific
request fields, so ``recorded`` mode takes both from the manifest: a replay then needs
no model configuration, and an investigation shows the model that answered it.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

#: The manifest's file name in the recordings directory.
MANIFEST_FILE = "manifest.json"

#: Version of the manifest format; a manifest with another version fails to load.
MANIFEST_SCHEMA_VERSION = 1


class ManifestError(ValueError):
    """The manifest cannot be used; the message names the file."""


class RecordingManifest(BaseModel):
    """``manifest.json``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    #: The chat model the recordings were made with (``BIANQUE_LLM_MODEL``).
    model: str
    #: Provider-specific request fields (``BIANQUE_LLM_EXTRA_BODY``).
    extra_body: dict[str, Any]
    #: Host of the model endpoint, without path or credentials.
    endpoint_host: str | None
    #: When the recording run finished.
    recorded_at: datetime
    #: The recorded cases, one directory each.
    cases: tuple[str, ...]
    #: SHA-256 of each prompt file, by file name.
    prompts: dict[str, str]
    #: SHA-256 of the canonical JSON of every registered tool's schema.
    tool_schemas_sha256: str


def load_manifest(recordings: Path) -> RecordingManifest | None:
    """Load ``<recordings>/manifest.json``; ``None`` when there is none.

    Raises:
        ManifestError: The file is unreadable, malformed or of another version.
    """
    path = recordings / MANIFEST_FILE
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ManifestError(f"{path}: cannot read the recording manifest: {exc}") from exc
    version = data.get("schema_version") if isinstance(data, dict) else None
    if version != MANIFEST_SCHEMA_VERSION:
        raise ManifestError(
            f"{path}: schema_version is {version!r}, expected {MANIFEST_SCHEMA_VERSION}"
        )
    try:
        return RecordingManifest.model_validate(data)
    except ValidationError as exc:
        raise ManifestError(f"{path}: {exc}") from exc


def write_manifest(recordings: Path, manifest: RecordingManifest) -> Path:
    """Write the manifest into ``recordings`` and return its path."""
    path = recordings / MANIFEST_FILE
    document = manifest.model_dump(mode="json")
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return path
