"""Evaluation metrics: pure functions over what one case's run left in the database.

``CaseRun`` holds the rows the run wrote, read back by ``bianque.evals.run``; every
function here computes from it without a database, so the definitions
are tested on their own. Accuracy metrics are reported only; the hard gates live in
``bianque.evals.run``.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from bianque.evals.cases import Case, EvidenceSpec, Question
from bianque.evals.report import (
    CaseResult,
    EvidenceMatch,
    GateStats,
    QuestionResult,
    Ratio,
    RootCauseResult,
    Summary,
)
from bianque.gate import OutputType

#: Output types whose generations are counted, in report order.
GATED_TYPES = (
    OutputType.PLAN,
    OutputType.FINDINGS,
    OutputType.ASSESSMENT,
    OutputType.REPORT,
    OutputType.FOLLOWUP_ANSWER,
)

#: Attempts of one generation of every output type but the report: one retry.
DEFAULT_ATTEMPTS = 2


@dataclass(frozen=True)
class EvidenceRow:
    """One evidence row: its ID, source, tool and the tool's arguments."""

    id: str
    source: str
    tool: str | None
    arguments: Mapping[str, Any]


@dataclass(frozen=True)
class CallRow:
    """One call record."""

    id: int
    agent: str
    kind: str
    name: str
    status: str
    error: str | None
    gate_result: Mapping[str, Any] | None
    tokens: int


@dataclass(frozen=True)
class ReportRow:
    """One report attempt."""

    attempt: int
    status: str
    body: Mapping[str, Any]


@dataclass(frozen=True)
class Answer:
    """The outcome of one follow-up question."""

    question: Question
    #: ``answered`` or the failure reason of the flow.
    outcome: str
    segments: Sequence[Mapping[str, Any]] = ()
    references: Sequence[str] = ()
    unknown: bool = False


@dataclass(frozen=True)
class CaseRun:
    """What one case's run left behind."""

    case: Case
    investigation_id: str
    status: str
    error: str | None
    duration_s: float
    tokens: int
    token_budget: int
    plan_sources: Sequence[str]
    rule_overrides: int
    evidence: Sequence[EvidenceRow]
    suggestion_ids: frozenset[str]
    calls: Sequence[CallRow]
    followup_calls: Sequence[CallRow]
    reports: Sequence[ReportRow]
    answers: Sequence[Answer] = field(default_factory=tuple)


# -- Report conclusions -------------------------------------------------------------


def conclusions(body: Mapping[str, Any]) -> list[list[str]]:
    """Return the citations of each conclusion of a report body.

    Conclusions are the timeline entries, the claims, the suggestions and an identified
    root cause. A malformed body yields what can be read.
    """
    found: list[list[str]] = []
    for key in ("timeline", "claims", "suggestions"):
        for item in body.get(key) or []:
            if isinstance(item, Mapping):
                found.append(_citations(item))
    cause = body.get("root_cause")
    if isinstance(cause, Mapping) and cause.get("status") == "identified":
        found.append(_citations(cause))
    return found


def _citations(item: Mapping[str, Any]) -> list[str]:
    citations = item.get("citations")
    return [str(c) for c in citations] if isinstance(citations, list) else []


def citation_completeness(run: CaseRun) -> Ratio:
    """Conclusions of the first report attempt whose citations are all evidence IDs."""
    first = next((r for r in run.reports if r.attempt == 1), None)
    if first is None:
        return Ratio.of(0, 0)
    evidence = {e.id for e in run.evidence}
    found = conclusions(first.body)
    valid = sum(1 for cited in found if cited and all(c in evidence for c in cited))
    return Ratio.of(valid, len(found))


def final_report(run: CaseRun) -> ReportRow | None:
    """The published or degraded report."""
    return next((r for r in run.reports if r.status in ("published", "degraded")), None)


def cited_evidence(body: Mapping[str, Any]) -> set[str]:
    """Every evidence ID a report body cites."""
    return {c for cited in conclusions(body) for c in cited} | set(
        _citations(body.get("root_cause") or {})
    )


# -- Root cause and evidence ----------------------------------------------------------


def _matches(spec: EvidenceSpec, rows: Iterable[EvidenceRow]) -> list[str]:
    return sorted(row.id for row in rows if spec.matches(row.tool, row.arguments))


def root_cause(run: CaseRun) -> RootCauseResult:
    """The final root cause against the case's expected root cause."""
    expected = run.case.expected
    final = final_report(run)
    actual: dict[str, Any] = dict((final.body.get("root_cause") or {}) if final else {})
    want = expected.root_cause
    if want.status == "unknown":
        correct = actual.get("status") == "unknown"
    else:
        target = actual.get("object") or {}
        assert want.object is not None
        correct = (
            actual.get("status") == "identified"
            and actual.get("category") == want.category
            and all(
                target.get(key) == getattr(want.object, key)
                for key in ("kind", "namespace", "name")
            )
        )
    cites: bool | None = None
    if expected.root_cause_evidence:
        cited = [row for row in run.evidence if row.id in set(_citations(actual))]
        cites = all(_matches(spec, cited) for spec in expected.root_cause_evidence)
    confidence_ok = (
        actual.get("confidence") not in expected.confidence_not if expected.confidence_not else None
    )
    missing_ok: bool | None = None
    if expected.missing_evidence_mentions:
        missing = " ".join(
            f"{item.get('what', '')} {item.get('reason', '')}"
            for item in ((final.body.get("missing_evidence") or []) if final else [])
            if isinstance(item, Mapping)
        ).lower()
        missing_ok = any(word.lower() in missing for word in expected.missing_evidence_mentions)
    return RootCauseResult(
        expected=want.model_dump(mode="json", exclude_none=True),
        actual={
            key: actual[key]
            for key in ("status", "category", "object", "confidence")
            if key in actual
        },
        correct=correct,
        cites_expected_evidence=cites,
        confidence_ok=confidence_ok,
        missing_evidence_ok=missing_ok,
    )


def required_evidence(run: CaseRun) -> tuple[Ratio, list[EvidenceMatch]]:
    """Expected evidence the final report cites, by tool and identity arguments."""
    final = final_report(run)
    cited_ids = cited_evidence(final.body) if final else set()
    cited = [row for row in run.evidence if row.id in cited_ids]
    matches = [
        EvidenceMatch(tool=spec.tool, args=dict(spec.args), matched=_matches(spec, cited))
        for spec in run.case.expected.required_evidence
    ]
    return Ratio.of(sum(1 for m in matches if m.matched), len(matches)), matches


# -- Evidence gate ----------------------------------------------------------------------


def gate_stats(calls: Sequence[CallRow], report_attempts: int) -> dict[str, GateStats]:
    """First-pass rate, retries and fallbacks of each output type.

    The calls of one agent at one call site are sequential, so a generation is a run of
    consecutive gated calls of the same agent and output type that ends with a pass, or
    with the last attempt allowed.
    """
    generations: dict[str, list[list[Mapping[str, Any]]]] = {t.value: [] for t in GATED_TYPES}
    open_runs: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for call in sorted(calls, key=lambda c: c.id):
        result = call.gate_result
        if result is None:
            continue
        output_type = str(result.get("output_type"))
        if output_type not in generations:
            continue
        limit = report_attempts if output_type == OutputType.REPORT.value else DEFAULT_ATTEMPTS
        key = (call.agent, output_type)
        current = open_runs.setdefault(key, [])
        current.append(result)
        if result.get("passed") or len(current) >= limit:
            generations[output_type].append(current)
            del open_runs[key]
    for (_, output_type), current in open_runs.items():
        generations[output_type].append(current)
    stats = {}
    for output_type, runs in generations.items():
        rejected: Counter[str] = Counter(
            violation["validator"]
            for run in runs
            for attempt in run
            for violation in attempt.get("violations") or []
        )
        stats[output_type] = GateStats(
            generations=len(runs),
            first_pass=Ratio.of(sum(1 for run in runs if run[0].get("passed")), len(runs)),
            attempts=sum(len(run) for run in runs),
            mean_retries=round(sum(len(run) - 1 for run in runs) / len(runs), 4) if runs else None,
            fallbacks=sum(1 for run in runs if not run[-1].get("passed")),
            rejected_by=dict(sorted(rejected.items())),
        )
    return stats


# -- Follow-up ----------------------------------------------------------------------------


def question_result(
    answer: Answer, evidence: Sequence[EvidenceRow], suggestions: frozenset[str]
) -> QuestionResult:
    """One question's answer against its annotation."""
    question = answer.question
    known = {row.id for row in evidence} | suggestions
    claimed = [s for s in answer.segments if not s.get("insufficient_evidence")]
    valid = sum(
        1 for s in claimed if s.get("citations") and all(c in known for c in s["citations"])
    )
    cited = {str(c) for s in answer.segments for c in s.get("citations") or []}
    hit: bool | None = None
    if question.expected_evidence:
        rows = [row for row in evidence if row.id in cited]
        hit = any(_matches(spec, rows) for spec in question.expected_evidence)
    return QuestionResult(
        id=question.id,
        question=question.question,
        outcome=answer.outcome,
        segments=len(answer.segments),
        cited_segments=Ratio.of(valid, len(claimed)),
        unknown=answer.unknown,
        expect_unknown=question.expect_unknown,
        expected_evidence_hit=hit,
        suggestion_cited=bool(cited & suggestions) if question.expected_suggestions else None,
        depends_on=question.depends_on,
    )


# -- Case and summary ---------------------------------------------------------------------


def case_result(run: CaseRun, *, report_attempts: int) -> CaseResult:
    """Every metric of one case."""
    coverage, matches = required_evidence(run)
    final = final_report(run)
    model_calls = [c for c in (*run.calls, *run.followup_calls) if c.kind == "model"]
    skills = Counter(c.name for c in run.calls if c.kind == "skill")
    return CaseResult(
        case=run.case.case,
        title=run.case.title,
        investigation_id=run.investigation_id,
        status=run.status,
        error=run.error,
        duration_s=round(run.duration_s, 1),
        tokens=run.tokens,
        token_budget=run.token_budget,
        model_calls=len(model_calls),
        tool_calls=sum(1 for c in run.calls if c.kind == "tool"),
        recording_misses=sum(
            1 for c in model_calls if (c.error or "").startswith("RecordingNotFoundError")
        ),
        plans=list(run.plan_sources),
        rule_overrides=run.rule_overrides,
        citation_completeness=citation_completeness(run),
        root_cause=root_cause(run),
        required_evidence=coverage,
        required_evidence_matches=matches,
        gate=gate_stats([*run.calls, *run.followup_calls], report_attempts),
        report_attempts=len(run.reports)
        - (1 if final is not None and final.status == "degraded" else 0),
        report_status=final.status if final else None,
        followup=[question_result(a, run.evidence, run.suggestion_ids) for a in run.answers],
        skills=dict(sorted(skills.items())),
    )


def _sum(ratios: Iterable[Ratio]) -> Ratio:
    items = list(ratios)
    return Ratio.of(sum(r.count for r in items), sum(r.total for r in items))


def summary(cases: Sequence[CaseResult]) -> Summary:
    """The metrics over all cases: counts summed, then divided."""
    questions = [q for case in cases for q in case.followup]
    expected = [q for q in questions if q.expected_evidence_hit is not None]
    unknown = [q for q in questions if q.expect_unknown]
    return Summary(
        cases=len(cases),
        root_cause_correct=Ratio.of(sum(1 for c in cases if c.root_cause.correct), len(cases)),
        citation_completeness=_sum(c.citation_completeness for c in cases),
        required_evidence=_sum(c.required_evidence for c in cases),
        gate_first_pass={
            t.value: _sum(c.gate[t.value].first_pass for c in cases) for t in GATED_TYPES
        },
        followup_cited_segments=_sum(q.cited_segments for q in questions),
        followup_expected_evidence=Ratio.of(
            sum(1 for q in expected if q.expected_evidence_hit), len(expected)
        ),
        followup_unknown=Ratio.of(sum(1 for q in unknown if q.unknown), len(unknown)),
        tokens=sum(c.tokens for c in cases),
        model_calls=sum(c.model_calls for c in cases),
        skill_activations=sum(sum((c.skills or {}).values()) for c in cases),
    )
