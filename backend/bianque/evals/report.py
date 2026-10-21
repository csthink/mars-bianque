"""The evaluation report: ``report.json`` and its human-readable ``report.md``.

The JSON is the record; its structure is the pydantic model ``EvalReport`` below, so a
report can be validated against the model or against its exported JSON Schema. The
Markdown is a summary for a reader and is rendered from the JSON alone.

Version 2 adds the skills each case loaded. A version 1 report still loads, with no
skill fields, so ``render_comparison`` can set a report made before skills existed
beside one made with them.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

#: Version of the report format.
REPORT_SCHEMA_VERSION: Literal[2] = 2

#: What a ``recorded`` result stands for (``docs/adr/0005-recorded-model-responses-for-eval.md``).
RECORDED_NOTE = (
    "Recorded mode replays the responses of one live run; the numbers describe that run, "
    "not every run of the same model."
)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Ratio(_Model):
    """A count over a total; ``value`` is ``None`` when the total is zero."""

    count: int
    total: int
    value: float | None

    @classmethod
    def of(cls, count: int, total: int) -> Ratio:
        """Return ``count / total`` rounded to four places."""
        return cls(count=count, total=total, value=round(count / total, 4) if total else None)


class GateStats(_Model):
    """How one output type fared at the evidence gate.

    A generation is one output and its retries; it passes first time when its first
    attempt passed, and falls back when every attempt was rejected.
    """

    generations: int
    first_pass: Ratio
    attempts: int
    mean_retries: float | None
    fallbacks: int
    rejected_by: dict[str, int]


class RootCauseResult(_Model):
    """The final report's root cause against the expected one."""

    expected: dict[str, Any]
    actual: dict[str, Any]
    correct: bool
    #: The root cause cites every ``root_cause_evidence`` of the case.
    cites_expected_evidence: bool | None
    #: S1-d: the confidence is none of ``confidence_not``.
    confidence_ok: bool | None
    #: S1-d: the missing evidence mentions one of ``missing_evidence_mentions``.
    missing_evidence_ok: bool | None


class EvidenceMatch(_Model):
    """One expected evidence and the cited evidence that matched it."""

    tool: str
    args: dict[str, Any]
    matched: list[str]


class QuestionResult(_Model):
    """One follow-up question and its answer."""

    id: str
    question: str
    #: ``answered``, or why there is no answer (``gate_rejected``, ``model_error``, ...).
    outcome: str
    segments: int
    #: Segments not marked as insufficient evidence, and those whose citations are all
    #: valid evidence or suggestion IDs.
    cited_segments: Ratio
    unknown: bool
    expect_unknown: bool
    #: The answer cites at least one expected evidence; ``None`` when none is expected.
    expected_evidence_hit: bool | None
    #: The answer cites a suggestion; ``None`` when none is expected.
    suggestion_cited: bool | None
    depends_on: str | None


class CaseResult(_Model):
    """Everything measured on one case."""

    case: str
    title: str
    investigation_id: str
    status: str
    error: str | None
    duration_s: float
    tokens: int
    token_budget: int
    model_calls: int
    tool_calls: int
    recording_misses: int
    plans: list[str]
    rule_overrides: int
    citation_completeness: Ratio
    root_cause: RootCauseResult
    required_evidence: Ratio
    required_evidence_matches: list[EvidenceMatch]
    gate: dict[str, GateStats]
    report_attempts: int
    report_status: str | None
    followup: list[QuestionResult]
    #: Activations of each skill, first loads and repeats; ``None`` in a version 1 report.
    skills: dict[str, int] | None = None


class HardGate(_Model):
    """A deterministic check; any failure fails the evaluation."""

    name: str
    passed: bool
    details: list[str]


class Summary(_Model):
    """The metrics over all cases."""

    cases: int
    root_cause_correct: Ratio
    citation_completeness: Ratio
    required_evidence: Ratio
    gate_first_pass: dict[str, Ratio]
    followup_cited_segments: Ratio
    followup_expected_evidence: Ratio
    followup_unknown: Ratio
    tokens: int
    model_calls: int
    #: Skill activations of every case; ``None`` in a version 1 report.
    skill_activations: int | None = None


class LLMContext(_Model):
    """The model that produced the numbers."""

    mode: Literal["live", "recorded", "scripted"]
    model: str
    extra_body: dict[str, Any]
    #: ``recorded`` mode: when the recordings were made, from the manifest.
    recorded_at: datetime | None
    note: str | None


class EvalReport(_Model):
    """``report.json``."""

    schema_version: Literal[1, 2]
    generated_at: datetime
    passed: bool
    llm: LLMContext
    embedding: dict[str, str]
    prompts: dict[str, str]
    tool_schemas_sha256: str
    network_isolated: bool | None
    hard_gates: list[HardGate]
    summary: Summary
    cases: list[CaseResult]
    retrieval: dict[str, Any]


def _ratio(ratio: Ratio) -> str:
    if ratio.value is None:
        return f"- ({ratio.count}/{ratio.total})"
    return f"{ratio.value:.2%} ({ratio.count}/{ratio.total})"


def _count(value: int | None) -> str:
    return "not recorded" if value is None else str(value)


def _flag(value: bool | None) -> str:
    return "-" if value is None else ("yes" if value else "no")


def _skills(skills: dict[str, int] | None) -> str:
    if skills is None:
        return "not recorded"
    return ", ".join(f"{name} ×{n}" if n > 1 else name for name, n in skills.items()) or "-"


def render_markdown(report: EvalReport) -> str:
    """Return the Markdown summary of a report."""
    llm = report.llm
    summary = report.summary
    lines = [
        "# Bianque evaluation",
        "",
        f"- Result: {'passed' if report.passed else 'FAILED'}",
        f"- Model mode: {llm.mode}; model: {llm.model or '-'}"
        + (f"; recorded at {llm.recorded_at.isoformat()}" if llm.recorded_at else ""),
        f"- Embeddings: {report.embedding.get('mode', '-')} ({report.embedding.get('model', '-')})",
        f"- Generated at {report.generated_at.isoformat()}",
    ]
    if report.network_isolated is not None:
        lines.append(f"- Network isolated: {_flag(report.network_isolated)}")
    if llm.note:
        lines += ["", f"> {llm.note}"]
    lines += [
        "",
        "## Hard gates",
        "",
        "| gate | result | details |",
        "| --- | --- | --- |",
        *(
            f"| {gate.name} | {'pass' if gate.passed else 'FAIL'} | "
            f"{'; '.join(gate.details) or '-'} |"
            for gate in report.hard_gates
        ),
        "",
        "## Summary",
        "",
        "| metric | value |",
        "| --- | --- |",
        f"| root cause correct | {_ratio(summary.root_cause_correct)} |",
        f"| citation completeness (first report) | {_ratio(summary.citation_completeness)} |",
        f"| required evidence coverage | {_ratio(summary.required_evidence)} |",
        *(
            f"| gate first pass: {name} | {_ratio(ratio)} |"
            for name, ratio in summary.gate_first_pass.items()
        ),
        f"| follow-up cited segments | {_ratio(summary.followup_cited_segments)} |",
        f"| follow-up expected evidence cited | {_ratio(summary.followup_expected_evidence)} |",
        f"| follow-up unknown answered as unknown | {_ratio(summary.followup_unknown)} |",
        f"| tokens | {summary.tokens} |",
        f"| model calls | {summary.model_calls} |",
        f"| skill activations | {_count(summary.skill_activations)} |",
        "",
        "## Cases",
        "",
        "| case | status | root cause | correct | citations | required evidence | "
        "report attempts | tokens | skills |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for case in report.cases:
        actual = case.root_cause.actual
        cause = actual.get("category") or actual.get("status") or "-"
        lines.append(
            f"| {case.case} | {case.status} | {cause} | {_flag(case.root_cause.correct)} | "
            f"{_ratio(case.citation_completeness)} | {_ratio(case.required_evidence)} | "
            f"{case.report_attempts} | {case.tokens} / {case.token_budget} | "
            f"{_skills(case.skills)} |"
        )
    lines += [
        "",
        "## Follow-up questions",
        "",
        "| case | question | outcome | cited segments | expected evidence | suggestion | "
        "unknown | expected unknown |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for case in report.cases:
        for q in case.followup:
            lines.append(
                f"| {case.case} | {q.id}{' (follows ' + q.depends_on + ')' if q.depends_on else ''}"
                f" | {q.outcome} | {_ratio(q.cited_segments)} | {_flag(q.expected_evidence_hit)}"
                f" | {_flag(q.suggestion_cited)} | {_flag(q.unknown)} | {_flag(q.expect_unknown)} |"
            )
    retrieval = report.retrieval.get("metrics", {})
    if retrieval:
        lines += [
            "",
            "## Retrieval",
            "",
            "| metric | value |",
            "| --- | --- |",
            *(f"| {name} | {value} |" for name, value in retrieval.items()),
        ]
    return "\n".join(lines) + "\n"


def write_report(out: Path, report: EvalReport) -> None:
    """Write ``report.json`` and ``report.md`` into ``out``."""
    out.mkdir(parents=True, exist_ok=True)
    document = report.model_dump(mode="json")
    (out / "report.json").write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n")
    (out / "report.md").write_text(render_markdown(report))


#: Per-case fields a recorded replay must reproduce from the live run it recorded.
_REPRODUCED = (
    "status",
    "tokens",
    "model_calls",
    "tool_calls",
    "plans",
    "rule_overrides",
    "citation_completeness",
    "root_cause",
    "required_evidence",
    "report_attempts",
    "report_status",
    "followup",
    "skills",
)


def compare_reports(live: EvalReport, replay: EvalReport) -> list[str]:
    """Return how a recorded replay differs from the live run it recorded; empty if not."""
    problems = []
    if replay.llm.mode != "recorded":
        problems.append(f"the replay ran in {replay.llm.mode} mode, not recorded")
    if live.llm.mode != "live":
        problems.append(f"the recording ran in {live.llm.mode} mode, not live")
    replayed = {case.case: case for case in replay.cases}
    for case in live.cases:
        other = replayed.get(case.case)
        if other is None:
            problems.append(f"{case.case}: not replayed")
            continue
        if other.recording_misses:
            problems.append(f"{case.case}: {other.recording_misses} requests had no recording")
        for name in _REPRODUCED:
            if getattr(case, name) != getattr(other, name):
                problems.append(f"{case.case}: {name} differs")
    return problems


def load_report(path: Path) -> EvalReport:
    """Read a ``report.json`` of format version 1 or 2."""
    return EvalReport.model_validate_json(path.read_text(encoding="utf-8"))


def _cause(case: CaseResult) -> str:
    actual = case.root_cause.actual
    label = actual.get("category") or actual.get("status") or "none"
    return f"{label}, {'correct' if case.root_cause.correct else 'wrong'}"


def _side(report: EvalReport) -> str:
    llm = report.llm
    return (
        f"{llm.model or '-'} in {llm.mode} mode, generated at {report.generated_at.isoformat()}"
        f" (report format {report.schema_version})"
    )


def render_comparison(base: EvalReport, new: EvalReport) -> str:
    """Set two reports side by side, case by case: root cause, citations, evidence, cost.

    The baseline is typically a report made before skills existed; its skill columns
    then say ``not recorded``.
    """
    newer = {case.case: case for case in new.cases}
    rows = []
    for case in base.cases:
        other = newer.get(case.case)
        if other is None:
            continue
        rows.append(
            f"| {case.case} | {_cause(case)} | {_cause(other)} | "
            f"{_ratio(case.citation_completeness)} | {_ratio(other.citation_completeness)} | "
            f"{_ratio(case.required_evidence)} | {_ratio(other.required_evidence)} | "
            f"{case.tokens} | {other.tokens} | {case.model_calls} | {other.model_calls} | "
            f"{_skills(case.skills)} | {_skills(other.skills)} |"
        )
    a, b = base.summary, new.summary
    rows.append(
        f"| all | {_ratio(a.root_cause_correct)} | {_ratio(b.root_cause_correct)} | "
        f"{_ratio(a.citation_completeness)} | {_ratio(b.citation_completeness)} | "
        f"{_ratio(a.required_evidence)} | {_ratio(b.required_evidence)} | "
        f"{a.tokens} | {b.tokens} | {a.model_calls} | {b.model_calls} | "
        f"{_count(a.skill_activations)} | {_count(b.skill_activations)} |"
    )
    lines = [
        "# Evaluation comparison",
        "",
        f"- Baseline: {_side(base)}",
        f"- New: {_side(new)}",
        "",
        "> Both reports come from one live run each; they do not show how the model "
        "behaves on every run. Tokens are those of the investigation; model calls include "
        "the follow-up questions.",
        "",
        "| case | root cause (baseline) | root cause (new) | citations (baseline) | "
        "citations (new) | required evidence (baseline) | required evidence (new) | "
        "tokens (baseline) | tokens (new) | model calls (baseline) | model calls (new) | "
        "skills (baseline) | skills (new) |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        *rows,
    ]
    return "\n".join(lines) + "\n"
