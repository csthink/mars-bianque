"""The repository's skills: valid, shared with Claude Code, and naming real tools only."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from bianque.redaction import RedactionEngine
from bianque.skills import SKILL_FILE, load_skills
from bianque.tools import registry

REPO = Path(__file__).resolve().parents[3]
SKILLS = REPO / ".agents" / "skills"
CLAUDE_SKILLS = REPO / ".claude" / "skills"

#: The prefixes of registered tool names; a word that starts with one looks like a tool.
TOOL_PREFIXES = sorted({definition.name.split("_", 1)[0] for definition in registry})
TOOL_LIKE = re.compile(rf"\b(?:{'|'.join(TOOL_PREFIXES)})_[a-z0-9_]+\b")

EXPECTED = (
    "attribute-gitops-change",
    "diagnose-config-error",
    "diagnose-oomkilled",
    "diagnose-probe-failure",
    "investigate-pod-restarts",
)


def test_the_repository_skills_pass_the_strict_validation() -> None:
    assert load_skills(SKILLS).names == EXPECTED


def test_claude_code_reads_the_same_directory_through_a_relative_symlink() -> None:
    assert CLAUDE_SKILLS.is_symlink()
    assert os.readlink(CLAUDE_SKILLS) == "../.agents/skills"
    assert CLAUDE_SKILLS.resolve() == SKILLS.resolve()


def test_no_skill_carries_scripts_or_executables() -> None:
    for path in SKILLS.rglob("*"):
        assert path.name != "scripts", path
        if path.is_file():
            assert not os.access(path, os.X_OK), path


def test_every_tool_named_in_a_body_is_a_registered_tool() -> None:
    names = {definition.name for definition in registry}
    for skill in load_skills(SKILLS):
        mentioned = set(TOOL_LIKE.findall(skill.body))
        assert mentioned, f"{skill.name} names no tool"
        assert mentioned <= names, f"{skill.name}: {sorted(mentioned - names)}"
        # Tool names carry no client prefix (Claude Code shows mcp__<server>__<tool>).
        assert "mcp__" not in skill.body


def test_bodies_reach_the_model_unchanged_by_redaction() -> None:
    engine = RedactionEngine()
    tools = frozenset(definition.name for definition in registry)
    for skill in load_skills(SKILLS):
        assert engine.detect(skill.body, preserve=tools) == [], skill.name
        assert engine.detect(skill.description, preserve=tools) == [], skill.name


def _agentskills() -> Path:
    return Path(sys.executable).parent / "agentskills"


@pytest.mark.parametrize("name", EXPECTED)
def test_the_reference_validator_accepts_each_skill(name: str) -> None:
    # skills-ref is a locked development dependency; its command is ``agentskills``.
    result = subprocess.run(
        [str(_agentskills()), "validate", str(SKILLS / name)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert (SKILLS / name / SKILL_FILE).is_file()
