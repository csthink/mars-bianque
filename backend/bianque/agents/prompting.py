"""Prompt construction: the prompt files and the fixed layout of the data sections.

Prompt text is part of the request key of recorded responses, so it is built the same
way on every run: no time and no investigation ID; evidence, findings,
failures and violations are sorted before they are joined; a tool error names its
category and object only. Each model call site has one prompt file in ``prompts/`` that
holds the role, the evidence discipline and the task. The data follows in one user
message made of sections with fixed headings; the scripted responder parses the same
headings (``bianque.agents.scripted_rules``).

The planning prompts add the skill catalog (``prompts/skills.md``) to the role text
when there are skills, and nothing about skills when there are none.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from string import Template
from typing import Any

from bianque.agents.domains import DOMAINS
from bianque.gate import Violation
from bianque.llm import RedactedPrompt, redact_prompt_for_llm
from bianque.redaction import RedactionEngine
from bianque.skills import SkillCatalog, render_catalog, skill_content
from bianque.tools import ToolRegistry

PROMPTS_DIR = Path(__file__).parent / "prompts"

#: Section headings of the user message.
EVIDENCE = "Evidence"
STEP = "Step"
FINDINGS = "Findings"
FAILED_CALLS = "Failed calls"
DOMAINS_HEADING = "Evidence domains"
ASSESSMENT = "Assessment"
KNOWLEDGE = "Knowledge"
REJECTION = "Rejected attempt"
REPORT = "Report"
SUGGESTIONS = "Suggestions"
SKILLS = "Skills"

#: Written where a section has no content.
NONE = "(none)"


@cache
def _read(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.md").read_text()


def prompt_text(name: str, **values: str) -> str:
    """Return a prompt file with the evidence discipline and ``values`` filled in.

    Trailing blank lines are dropped, so a placeholder filled with nothing at the end of
    a file leaves nothing behind.
    """
    text = Template(_read(name)).substitute(discipline=_read("discipline").strip(), **values)
    return text.rstrip()


def skills_prompt(catalog: SkillCatalog) -> str:
    """The skill part of a planning prompt: what skills are and the catalog; empty if none."""
    if not catalog:
        return ""
    return Template(_read("skills")).substitute(catalog=render_catalog(catalog)).strip()


def render_skills(catalog: SkillCatalog, loaded: Collection[str]) -> str:
    """Render the loaded skills' content, sorted by name; the lines inside the tags indented.

    The indentation by two spaces keeps the headings of a skill's body from reading as
    sections of the prompt.
    """
    blocks = []
    for skill in catalog:
        if skill.name in loaded:
            first, *inner, last = skill_content(skill).splitlines()
            indented = [f"  {line}" if line.strip() else "" for line in inner]
            blocks.append("\n".join([first, *indented, last]))
    return "\n\n".join(blocks)


def prompt_hashes() -> dict[str, str]:
    """SHA-256 of every prompt file, by file name, for reports and the recording manifest."""
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(PROMPTS_DIR.glob("*.md"))
    }


@dataclass(frozen=True)
class EvidenceView:
    """One piece of evidence as a prompt shows it."""

    id: str
    source: str
    tool: str | None
    text: str
    identifiers: tuple[str, ...] = ()


@dataclass(frozen=True)
class SuggestionView:
    """A remediation suggestion of the published report, as a prompt shows it."""

    id: str
    seq: int
    risk: str
    action: str
    command: str | None


@dataclass(frozen=True)
class FindingsView:
    """The findings of one step, in plan order."""

    plan_version: int
    seq: int
    domain: str
    goal: str
    findings: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True)
class FailureView:
    """A failed evidence tool call, or a failed sub-agent (``tool`` is ``None``)."""

    domain: str
    tool: str | None
    error: str


def section(heading: str, body: str) -> str:
    """Return one section of the user message."""
    return f"## {heading}\n{body.strip() or NONE}"


def evidence_header(item: EvidenceView) -> str:
    """Return the first line of one evidence entry.

    Names stand alone between spaces: the redaction's entropy check reads a run of
    characters without spaces as one token, and ``tool=k8s_get_pod_status`` would be
    long enough to be masked.
    """
    return f"[{item.id}] source: {item.source}; tool: {item.tool or '-'}"


def render_evidence(items: Iterable[EvidenceView]) -> str:
    """Render evidence entries, sorted by ID: a header line, then the redacted text."""
    return "\n\n".join(
        f"{evidence_header(item)}\n{item.text}" for item in sorted(items, key=lambda e: e.id)
    )


def render_findings(items: Iterable[FindingsView]) -> str:
    """Render the findings of each step in plan order."""
    blocks = []
    for item in sorted(items, key=lambda f: (f.plan_version, f.seq)):
        lines = [f"Plan {item.plan_version} step {item.seq} ({item.domain}): {item.goal}"]
        lines += [
            f"- {finding['statement']} [{', '.join(finding['citations'])}]"
            for finding in item.findings
        ] or ["- no findings"]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def render_failures(items: Iterable[FailureView]) -> str:
    """Render failed calls, sorted by domain, tool and error."""
    rows = sorted({(f.domain, f.tool or "", f.error) for f in items})
    return "\n".join(
        f"- {tool or f'the {domain} evidence agent'} ({domain} domain): {error}"
        for domain, tool, error in rows
    )


def render_violations(violations: Iterable[Violation]) -> str:
    """Render gate violations, sorted, without any text of the rejected output."""
    return "\n".join(f"- {v.validator} at {v.path}: {v.code}" for v in sorted(set(violations)))


def render_domains(tools: ToolRegistry, *, first_round: bool) -> str:
    """Render the evidence domains and their tool names in declaration order.

    Args:
        tools: The registry the tool names come from.
        first_round: Mark the domains a first plan may not use.
    """
    lines = []
    for item in DOMAINS:
        names = ", ".join(spec.name for spec in tools.specs(domain=item.name))
        note = "" if not first_round or item.first_round else " (not in the first round)"
        lines.append(f"- {item.name}{note}: {names}")
    return "\n".join(lines)


def render_step(goal: str, suggested_tools: Sequence[str], expected: Sequence[str]) -> str:
    """Render the step a sub-agent works on."""
    return "\n".join(
        [
            f"Goal: {goal}",
            f"Suggested tools: {', '.join(suggested_tools) or NONE}",
            f"Expected evidence: {' '.join(expected) or NONE}",
        ]
    )


def render_suggestions(items: Iterable[SuggestionView]) -> str:
    """Render remediation suggestions by position: ID, risk, action and command."""
    return "\n".join(
        f"- [{item.id}] risk: {item.risk}; action: {item.action}; command: {item.command or NONE}"
        for item in sorted(items, key=lambda s: s.seq)
    )


def render_answer(segments: Iterable[Mapping[str, Any]]) -> str:
    """Render the paragraphs of an earlier answer, each followed by what it cites."""
    blocks = []
    for segment in segments:
        if segment.get("insufficient_evidence"):
            mark = "(insufficient evidence)"
        else:
            mark = f"[{', '.join(segment.get('citations') or [])}]"
        blocks.append(f"{segment['text']} {mark}")
    return "\n\n".join(blocks)


def render_json(value: Any) -> str:
    """Render a value as compact JSON with sorted keys."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def build_prompt(
    system: str,
    sections: Sequence[str],
    *,
    engine: RedactionEngine,
    preserve: Collection[str],
) -> RedactedPrompt:
    """Return the redacted two-message prompt: the role text and the data sections."""
    return redact_prompt_for_llm(
        [("system", system), ("user", "\n\n".join(sections))], engine=engine, preserve=preserve
    )
