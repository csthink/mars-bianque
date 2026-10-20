"""Request and response models of the HTTP interface.

A response model is the only statement of what an endpoint returns. The body of a
rejected report, the input and output summaries of a model call and the raw content of
evidence are not fields of any model here, so they cannot be returned: a rejected output
is shown only by the names of the validators that rejected it.

Request models forbid unknown fields: a decision has no field for who made it, and a
request that tries to name one is rejected rather than ignored.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints


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
    decision_comment: str | None


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
    evidence it produced. A knowledge call (the investigation's runbook retrieval) shows
    its query and the chunks found; knowledge is not model output. A model call shows
    its tokens and gate result only.
    """

    id: int
    agent: str
    kind: Literal["model", "tool", "knowledge"]
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
    knowledge_calls: int
    tokens_in: int
    tokens_out: int
    latency_ms: int
    elapsed_ms: int


class CallList(BaseModel):
    """The calls of an investigation in the order they started, with totals."""

    calls: list[CallView]
    totals: CallTotals


#: The longest decision comment, the same bound as call record summaries.
MAX_COMMENT_CHARS = 2000


def _blank_is_none(value: str) -> str | None:
    return value or None


class LoginRequest(BaseModel):
    """A demo account login."""

    model_config = ConfigDict(extra="forbid")

    username: str = Field(max_length=64)
    password: str = Field(max_length=1024)


class SessionView(BaseModel):
    """Who is logged in."""

    username: str


class DecisionRequest(BaseModel):
    """A decision on a suggestion. Who decides comes from the session, not the body."""

    model_config = ConfigDict(extra="forbid")

    decision: Literal["confirmed", "rejected"]
    comment: (
        Annotated[
            str,
            StringConstraints(strip_whitespace=True, max_length=MAX_COMMENT_CHARS),
            AfterValidator(_blank_is_none),
        ]
        | None
    ) = None


class AlreadyDecided(BaseModel):
    """The 409 answer: the suggestion was decided before, and how."""

    detail: Literal["suggestion already decided"] = "suggestion already decided"
    state: Literal["confirmed", "rejected"]


class DeliveredAlertView(BaseModel):
    """What the webhook did with one alert."""

    fingerprint: str
    status: str
    investigation_id: str | None
    deduplicated: bool


class WebhookResult(BaseModel):
    """The alerts of one webhook delivery, in their order."""

    alerts: list[DeliveredAlertView]


class TriggerRequest(BaseModel):
    """A manual investigation: replay a case's evidence pack, or an alert received before."""

    model_config = ConfigDict(extra="forbid")

    case_id: str | None = Field(default=None, max_length=64)
    alert_id: int | None = None


class TriggerResult(BaseModel):
    """The investigation started; it runs in the background."""

    investigation_id: str


class FollowupSessionView(BaseModel):
    """A follow-up session."""

    id: int
    investigation_id: str
    created_by: str
    created_at: datetime


class AnswerSegmentView(BaseModel):
    """One paragraph of an answer that passed the evidence gate."""

    text: str
    citations: list[str]
    insufficient_evidence: bool


class FollowupMessageView(BaseModel):
    """A question, or the answer to it. Only answers that passed the gate exist."""

    id: int
    seq: int
    role: Literal["user", "assistant"]
    content: str
    segments: list[AnswerSegmentView]
    citations: list[str]
    references: list[str]
    unknown: bool
    created_at: datetime


class FollowupSessionSummary(FollowupSessionView):
    """A follow-up session in the list of an investigation's sessions."""

    turns: int


class FollowupSessionList(BaseModel):
    """The follow-up sessions of an investigation, newest first."""

    sessions: list[FollowupSessionSummary]


class FollowupSessionDetail(BaseModel):
    """A follow-up session with its messages in order."""

    session: FollowupSessionView
    messages: list[FollowupMessageView]


#: The longest follow-up question.
MAX_QUESTION_CHARS = 2000


class QuestionRequest(BaseModel):
    """A follow-up question."""

    model_config = ConfigDict(extra="forbid")

    question: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_QUESTION_CHARS)
    ]


class RecordedQuestionView(BaseModel):
    """A question of the case's question set."""

    id: str
    question: str


class QuestionList(BaseModel):
    """The questions of the case; when ``recorded_only``, only these, in this order."""

    recorded_only: bool
    questions: list[RecordedQuestionView]


class NotRecorded(BaseModel):
    """The 422 answer in ``recorded`` mode: the question the session may ask next."""

    detail: Literal["question not recorded"] = "question not recorded"
    expected: str | None
