"""Response models of the read interface.

A response model is the only statement of what an endpoint returns. The body of a
rejected report, the input and output summaries of a model call and the raw content of
evidence are not fields of any model here, so they cannot be returned: a rejected output
is shown only by the names of the validators that rejected it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel


class InvestigationSummary(BaseModel):
    """One row of the investigation list."""

    id: str
    status: str
    trigger: str
    case_id: str | None
    alert_name: str | None
    namespace: str | None
    pod: str | None
    started_at: datetime
    finished_at: datetime | None
    report_status: Literal["published", "degraded"] | None


class InvestigationList(BaseModel):
    """The most recently started investigations first."""

    investigations: list[InvestigationSummary]


class InvestigationInfo(BaseModel):
    """The investigation row."""

    id: str
    status: str
    trigger: str
    case_id: str | None
    tool_backend: str
    llm_mode: str
    llm_model: str | None
    token_budget: int
    tokens_used: int
    started_at: datetime
    finished_at: datetime | None
    error: str | None


class AlertView(BaseModel):
    """The alert that started the investigation, and the evidence that holds it."""

    name: str | None
    status: str
    fingerprint: str
    labels: dict[str, Any]
    annotations: dict[str, Any]
    starts_at: datetime
    received_at: datetime
    evidence_id: str | None


class FindingView(BaseModel):
    """A finding of a sub-agent; it passed the evidence gate before it was stored."""

    statement: str
    citations: list[str]


class StepView(BaseModel):
    """One step of a plan version."""

    id: int
    seq: int
    domain: str
    goal: str
    agent: str
    suggested_tools: list[str]
    expected_evidence: list[str]
    status: str
    findings: list[FindingView]
    started_at: datetime | None
    finished_at: datetime | None


class PlanView(BaseModel):
    """One plan version with its steps."""

    id: int
    version: int
    source: str
    rationale: str
    created_at: datetime
    steps: list[StepView]


class AssessmentView(BaseModel):
    """The judgement of one round."""

    round: int
    sufficient: bool
    reason: str
    hypothesis_category: str | None
    missing: list[str]
    rule_override: bool
    created_at: datetime


class EvidenceView(BaseModel):
    """One piece of evidence, as redacted text."""

    id: str
    step_id: int | None
    call_id: int | None
    source: str
    tool: str | None
    collected_at: datetime
    redacted_content: str
    arguments: dict[str, Any]


class ReportView(BaseModel):
    """The published or degraded report; its body passed the evidence gate."""

    id: int
    attempt: int
    status: Literal["published", "degraded"]
    body: dict[str, Any]
    created_at: datetime


class RejectedAttempt(BaseModel):
    """A rejected report attempt, shown only by the validators that rejected it."""

    attempt: int
    validators: list[str]
    created_at: datetime


class SuggestionView(BaseModel):
    """A remediation suggestion and the decision on it."""

    id: str
    seq: int
    action: str
    command: str | None
    risk: str
    citations: list[str]
    state: Literal["pending", "confirmed", "rejected"]
    decided_by: str | None
    decided_at: datetime | None


class InvestigationDetail(BaseModel):
    """Everything the detail page shows, in one response."""

    investigation: InvestigationInfo
    alert: AlertView | None
    plans: list[PlanView]
    assessments: list[AssessmentView]
    evidence: list[EvidenceView]
    report: ReportView | None
    rejected_attempts: list[RejectedAttempt]
    suggestions: list[SuggestionView]


class GateView(BaseModel):
    """The evidence gate result of a model call: passed, or the rejecting validators."""

    passed: bool
    validators: list[str]


class CallView(BaseModel):
    """One call record.

    A tool call shows its redacted input and output summaries, its error and the
    evidence it produced. A model call shows its tokens and gate result only.
    """

    id: int
    agent: str
    kind: Literal["model", "tool"]
    name: str
    caller: str | None
    started_at: datetime
    latency_ms: int | None
    status: str
    retries: int
    input_summary: str | None
    output_summary: str | None
    error: str | None
    evidence_ids: list[str]
    tokens_in: int | None
    tokens_out: int | None
    gate: GateView | None


class CallTotals(BaseModel):
    """Totals over the calls of an investigation.

    ``elapsed_ms`` runs from the investigation start to its end, or to the end of the
    last call while it is still running.
    """

    calls: int
    model_calls: int
    tool_calls: int
    tokens_in: int
    tokens_out: int
    latency_ms: int
    elapsed_ms: int


class CallList(BaseModel):
    """The calls of an investigation in the order they started, with totals."""

    calls: list[CallView]
    totals: CallTotals
