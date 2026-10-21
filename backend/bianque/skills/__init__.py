"""Skills: how Bianque investigates one kind of failure, written in the Agent Skills format.

The skills live in ``.agents/skills/<name>/SKILL.md`` at the repository root, where
other agent clients find them too. The investigation agent loads them while it plans:
the planning prompt lists the catalog, and the model loads a body with
``activate_skill``. A skill is neither evidence nor knowledge; the evidence gate keeps
skill names out of citations and references.

This package reads files only; it imports no other Bianque package (an import-linter
contract).
"""

from bianque.skills.activation import (
    ACTIVATE_DESCRIPTION,
    ACTIVATE_SKILL,
    Activation,
    activate,
    activation_parameters,
    render_catalog,
    skill_content,
)
from bianque.skills.loader import SKILL_FILE, Skill, SkillCatalog, SkillError, load_skills

__all__ = [
    "ACTIVATE_DESCRIPTION",
    "ACTIVATE_SKILL",
    "SKILL_FILE",
    "Activation",
    "Skill",
    "SkillCatalog",
    "SkillError",
    "activate",
    "activation_parameters",
    "load_skills",
    "render_catalog",
    "skill_content",
]
