"""Activating a skill: the function the planning model calls, and what it returns.

The catalog (names and descriptions) is all the planning prompt shows; the model loads a
body by calling ``activate_skill`` with a name from the enumeration of the catalog's
names. The body comes back as the function result, without its front matter and
wrapped in ``<skill_content>``. A skill already loaded in the investigation is not
injected again; the result only says so.

``activate_skill`` is not a registered tool: it collects no evidence, its result is not
stored as evidence, and neither the evidence sub-agents nor MCP clients see it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any

from bianque.skills.loader import Skill, SkillCatalog

#: The function that loads a skill's body.
ACTIVATE_SKILL = "activate_skill"
ACTIVATE_DESCRIPTION = (
    "Load the instructions of one skill from the skill catalog, by its name. A skill "
    "describes how to investigate one kind of failure; it is not evidence."
)

#: The output summary of a call record says which of the two an activation was.
FIRST_LOAD = "first load"
ALREADY_LOADED = "already loaded"


def activation_parameters(catalog: SkillCatalog) -> dict[str, Any]:
    """The JSON Schema of ``activate_skill``'s arguments: one name of the catalog."""
    return {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "enum": list(catalog.names),
                "description": "The name of the skill to load.",
            }
        },
        "required": ["name"],
        "additionalProperties": False,
    }


def render_catalog(catalog: SkillCatalog) -> str:
    """The catalog as the prompt shows it: ``- <name>: <description>``, by name."""
    return "\n".join(f"- {skill.name}: {skill.description}" for skill in catalog)


def skill_content(skill: Skill) -> str:
    """A skill's body as the model receives it.

    Files under ``references/`` are named, not read; the model cannot read files during
    planning, and none of the repository's skills has any.
    """
    parts = [f'<skill_content name="{skill.name}">', skill.body]
    if skill.references:
        parts.append("References (not loaded): " + ", ".join(skill.references))
    parts.append("</skill_content>")
    return "\n".join(parts)


@dataclass(frozen=True)
class Activation:
    """The outcome of one ``activate_skill`` call.

    Attributes:
        name: The skill, or ``None`` when the call named no skill of the catalog.
        result: The function result the model receives.
        first: The body was loaded now, not earlier in the investigation.
        digest: The first 12 hexadecimal digits of the SHA-256 of the skill's content.
    """

    name: str | None
    result: str
    first: bool = False
    digest: str = ""

    @property
    def summary(self) -> str:
        """The call record's output summary: the digest and whether it was a first load."""
        return f"sha256 {self.digest}; {FIRST_LOAD if self.first else ALREADY_LOADED}"


def activate(catalog: SkillCatalog, loaded: Collection[str], arguments: str) -> Activation:
    """Answer one ``activate_skill`` call.

    Args:
        catalog: The skills.
        loaded: The skills already loaded in this investigation.
        arguments: The call's arguments as the model sent them.

    Returns:
        The body of a skill not loaded yet; a note for a skill already loaded; a
        rejection naming the valid names when the call names no skill of the catalog.
        The rejection never repeats what the model sent.
    """
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError:
        parsed = None
    name = parsed.get("name") if isinstance(parsed, dict) else None
    skill = catalog.get(name) if isinstance(name, str) else None
    if skill is None:
        return Activation(
            None,
            f"Not loaded: {ACTIVATE_SKILL} takes the name of one skill of the catalog: "
            + ", ".join(catalog.names)
            + ".",
        )
    content = skill_content(skill)
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
    if skill.name in loaded:
        return Activation(
            skill.name,
            f"Skill {skill.name} is already loaded; its instructions are above.",
            first=False,
            digest=digest,
        )
    return Activation(skill.name, content, first=True, digest=digest)
