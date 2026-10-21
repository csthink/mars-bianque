"""Reading the skills directory and validating every skill strictly.

A skill is a directory with a ``SKILL.md``: YAML front matter (``name`` and
``description``, and optionally the other fields of the Agent Skills format), then a
Markdown body. Bianque loads only the skills of its own repository, so it does not
tolerate what the format's lenient loading would: any rule broken fails the load, and
the message names the skill directory and the rule. The rules:

- the directory holds only skill directories (and hidden entries, which are skipped),
  each with a ``SKILL.md``;
- the front matter is a mapping of the fields of the format and nothing else;
- ``name`` has 1 to 64 characters, lowercase letters, digits and single hyphens, no
  hyphen at either end, and equals the directory name;
- ``description`` has 1 to 1024 characters; ``compatibility`` at most 500;
  ``metadata`` maps strings to strings;
- the body is not empty, has fewer than 500 lines and at most 20000 characters (the
  format recommends fewer than 5000 tokens, about four characters each);
- there is no ``scripts/`` directory and no executable file: a skill only instructs.
"""

from __future__ import annotations

import hashlib
import re
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

#: The file that makes a directory a skill.
SKILL_FILE = "SKILL.md"

#: Front matter fields of the Agent Skills format.
ALLOWED_FIELDS = frozenset(
    {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
)

#: A skill name: lowercase letters and digits in groups joined by single hyphens.
NAME_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")

MAX_NAME = 64
MAX_DESCRIPTION = 1024
MAX_COMPATIBILITY = 500
#: The body must have fewer lines than this.
MAX_BODY_LINES = 500
MAX_BODY_CHARACTERS = 20000

#: The directory of files a body may name; they are listed, never read.
REFERENCES_DIR = "references"


class SkillError(ValueError):
    """A skill breaks a rule of the format; the message names the skill and the rule."""


@dataclass(frozen=True)
class Skill:
    """One validated skill.

    Attributes:
        name: The skill name, equal to its directory name.
        description: What the skill is for and when to use it.
        body: The Markdown after the front matter, without surrounding blank lines.
        path: The ``SKILL.md`` file.
        sha256: SHA-256 of the bytes of ``SKILL.md``.
        references: Files under ``references/``, relative to the skill directory, sorted.
    """

    name: str
    description: str
    body: str
    path: Path
    sha256: str
    references: tuple[str, ...] = ()


@dataclass(frozen=True)
class SkillCatalog:
    """The skills of one directory, sorted by name."""

    skills: tuple[Skill, ...] = ()

    def __bool__(self) -> bool:
        """Whether there is any skill."""
        return bool(self.skills)

    def __iter__(self) -> Iterator[Skill]:
        """Iterate the skills in name order."""
        return iter(self.skills)

    @property
    def names(self) -> tuple[str, ...]:
        """The skill names in order."""
        return tuple(skill.name for skill in self.skills)

    def get(self, name: str) -> Skill | None:
        """Return the skill called ``name``, or ``None``."""
        return next((skill for skill in self.skills if skill.name == name), None)

    def hashes(self) -> dict[str, str]:
        """SHA-256 of each ``SKILL.md``, keyed by ``<name>/SKILL.md``."""
        return {f"{skill.name}/{SKILL_FILE}": skill.sha256 for skill in self.skills}


def _fail(directory: Path, rule: str) -> SkillError:
    return SkillError(f"skill {directory}: {rule}")


def _split(directory: Path, text: str) -> tuple[dict[str, Any], str]:
    """Split ``SKILL.md`` into its front matter mapping and its body."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise _fail(directory, f"{SKILL_FILE} must start with a front matter line '---'")
    end = next((i for i, line in enumerate(lines[1:], start=1) if line.strip() == "---"), None)
    if end is None:
        raise _fail(directory, "the front matter has no closing line '---'")
    try:
        fields = yaml.safe_load("\n".join(lines[1:end]))
    except yaml.YAMLError as exc:
        raise _fail(directory, f"the front matter is not valid YAML: {exc}") from exc
    if not isinstance(fields, dict):
        raise _fail(directory, "the front matter must be a mapping")
    return fields, "\n".join(lines[end + 1 :]).strip("\n")


def _text(directory: Path, fields: dict[str, Any], key: str, limit: int) -> str:
    value = fields.get(key)
    if not isinstance(value, str) or not value.strip():
        raise _fail(directory, f"field '{key}' must be a non-empty string")
    if len(value) > limit:
        raise _fail(directory, f"field '{key}' has {len(value)} characters, at most {limit}")
    return value


def _check_fields(directory: Path, fields: dict[str, Any]) -> tuple[str, str]:
    unknown = sorted(str(key) for key in fields if key not in ALLOWED_FIELDS)
    if unknown:
        raise _fail(
            directory,
            f"unknown front matter fields {', '.join(unknown)}; "
            f"allowed are {', '.join(sorted(ALLOWED_FIELDS))}",
        )
    name = _text(directory, fields, "name", MAX_NAME)
    if not NAME_PATTERN.fullmatch(name):
        raise _fail(
            directory,
            f"name {name!r} must be lowercase letters and digits joined by single hyphens",
        )
    if name != directory.name:
        raise _fail(directory, f"name {name!r} must equal the directory name")
    description = _text(directory, fields, "description", MAX_DESCRIPTION)
    if "compatibility" in fields:
        _text(directory, fields, "compatibility", MAX_COMPATIBILITY)
    metadata = fields.get("metadata", {})
    if not isinstance(metadata, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in metadata.items()
    ):
        raise _fail(directory, "field 'metadata' must map strings to strings")
    return name, " ".join(description.split())


def _check_body(directory: Path, body: str) -> None:
    if not body.strip():
        raise _fail(directory, "the body is empty")
    lines = len(body.splitlines())
    if lines >= MAX_BODY_LINES:
        raise _fail(directory, f"the body has {lines} lines, fewer than {MAX_BODY_LINES} allowed")
    if len(body) > MAX_BODY_CHARACTERS:
        raise _fail(
            directory,
            f"the body has {len(body)} characters, at most {MAX_BODY_CHARACTERS} characters",
        )


def _check_files(directory: Path) -> tuple[str, ...]:
    """Refuse scripts and executables; return the files under ``references/``."""
    if (directory / "scripts").exists():
        raise _fail(directory, "a skill has no scripts directory")
    references = []
    for path in sorted(directory.rglob("*")):
        mode = path.lstat().st_mode
        if stat.S_ISREG(mode) and mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
            raise _fail(directory, f"{path.relative_to(directory)} is executable")
        relative = path.relative_to(directory)
        if stat.S_ISREG(mode) and relative.parts[0] == REFERENCES_DIR:
            references.append(relative.as_posix())
    return tuple(references)


def load_skill(directory: Path) -> Skill:
    """Load and validate the skill in ``directory``.

    Raises:
        SkillError: The skill breaks a rule; the message names the directory.
    """
    path = directory / SKILL_FILE
    if not path.is_file():
        raise _fail(directory, f"no {SKILL_FILE}")
    data = path.read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _fail(directory, f"{SKILL_FILE} is not UTF-8") from exc
    fields, body = _split(directory, text)
    name, description = _check_fields(directory, fields)
    _check_body(directory, body)
    references = _check_files(directory)
    return Skill(
        name=name,
        description=description,
        body=body,
        path=path,
        sha256=hashlib.sha256(data).hexdigest(),
        references=references,
    )


def load_skills(directory: Path) -> SkillCatalog:
    """Load every skill of ``directory``, sorted by name.

    An empty directory has no skills. A directory that does not exist is a
    configuration error, not an empty catalog.

    Raises:
        SkillError: The directory does not exist, holds something that is not a skill
            directory, or a skill breaks a rule.
    """
    if not directory.is_dir():
        raise SkillError(f"the skills directory {directory} does not exist")
    skills = []
    # Hidden entries (``.DS_Store`` and the like) are left by file browsers, not authors.
    for entry in sorted(e for e in directory.iterdir() if not e.name.startswith(".")):
        if not entry.is_dir():
            raise SkillError(
                f"the skills directory {directory} holds {entry.name}, which is not a skill "
                "directory"
            )
        skills.append(load_skill(entry))
    return SkillCatalog(tuple(sorted(skills, key=lambda skill: skill.name)))
