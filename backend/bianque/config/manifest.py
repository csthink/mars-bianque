"""The recording manifest: what the committed recordings were recorded with.

``eval/recordings/manifest.json`` sits beside the case directories of recorded
responses. It is written by the recording run (``make record``) once every case has been
recorded and replayed. The request key covers the model name and the provider-specific
request fields, so ``recorded`` mode takes both from the manifest: a replay then needs
no model configuration, and an investigation shows the model that answered it.

The prompt files and the skills are part of every request, so they must be the files
the recordings were made with: ``check_files`` compares their hashes with the manifest,
and ``recorded`` mode does so at startup (``bianque.agents.startup``), naming the first
file that differs instead of failing later on a request that has no recording.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

#: The manifest's file name in the recordings directory.
MANIFEST_FILE = "manifest.json"

#: Version of the manifest format; a manifest with another version fails to load.
MANIFEST_SCHEMA_VERSION = 2


class ManifestError(ValueError):
    """The manifest cannot be used; the message names the file."""


class RecordingManifest(BaseModel):
    """``manifest.json``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[2]
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
    #: SHA-256 of each skill's ``SKILL.md``, by ``<name>/SKILL.md``.
    skills: dict[str, str]
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


def _differences(kind: str, recorded: Mapping[str, str], current: Mapping[str, str]) -> list[str]:
    problems = []
    for name in sorted(set(recorded) | set(current)):
        if name not in recorded:
            problems.append(f"{kind}{name} is not in the recording manifest")
        elif name not in current:
            problems.append(f"{kind}{name} is in the recording manifest but not on disk")
        elif recorded[name] != current[name]:
            problems.append(f"{kind}{name} differs from the file the recordings were made with")
    return problems


def check_files(
    manifest: RecordingManifest,
    path: Path,
    *,
    prompts: Mapping[str, str],
    skills: Mapping[str, str],
    skills_dir: Path,
) -> None:
    """Check that the prompt files and the skills are those the recordings were made with.

    Args:
        manifest: The loaded manifest.
        path: The manifest file, for the message.
        prompts: SHA-256 of each prompt file now, by file name.
        skills: SHA-256 of each ``SKILL.md`` now, by ``<name>/SKILL.md``.
        skills_dir: The skills directory, for the message.

    Raises:
        ManifestError: A file differs, is missing or is new; the message names the first.
    """
    problems = _differences("bianque/agents/prompts/", manifest.prompts, prompts)
    problems += _differences(f"{skills_dir}/", manifest.skills, skills)
    if problems:
        raise ManifestError(
            f"{problems[0]} ({path}); record again with make record"
            + (f", {len(problems) - 1} more" if len(problems) > 1 else "")
        )
