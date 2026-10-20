"""Schemas of the five model output types the evidence gate validates.

Each output type is a pydantic model. The same model is the function schema the model
is forced to call (structured output by function calling) and the ``schema`` validator
of the gate, so the field names here are the argument names the model sees.

Every model forbids unknown fields and is immutable. Every field of the report is
required: a report is published only when all its sections are present, and a section
may be an empty list.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

#: Version of the report schema, stored with every report.
SchemaVersion = Literal["1"]
SCHEMA_VERSION: SchemaVersion = "1"

#: Root cause categories. The evaluation judges a root cause by its category and object.
RootCauseCategory = Literal[
    "config_error",
    "resource_limit",
    "probe_misconfig",
    "image_error",
    "dependency_unavailable",
    "code_defect",
    "other",
]

#: ``read_only`` actions only read; ``change`` actions modify something and need a person.
Risk = Literal["read_only", "change"]

Confidence = Literal["high", "medium", "low"]

#: A string that must not be empty.
Text = Annotated[str, Field(min_length=1)]

#: Evidence IDs (``ev_``); a follow-up answer may also cite suggestion IDs (``sg_``).
Citations = Annotated[list[Text], Field(description="IDs of the evidence this item rests on.")]


class _Output(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TimelineEntry(_Output):
    """One event of the incident, in time order."""

    time: AwareDatetime
    event: Text
    citations: Citations


class RootCauseObject(_Output):
    """The Kubernetes object the root cause is in, and optionally the field."""

    kind: Text
    namespace: Text
    name: Text
    field: Text | None = None


class RootCause(_Output):
    """The root cause, or ``unknown`` when the evidence does not establish one."""

    status: Literal["identified", "unknown"]
    category: RootCauseCategory | None = None
    object: RootCauseObject | None = None
    statement: Text
    citations: Citations
    confidence: Confidence

    @model_validator(mode="after")
    def _identified_names_category_and_object(self) -> Self:
        if self.status == "identified" and (self.category is None or self.object is None):
            raise ValueError("an identified root cause needs a category and an object")
        return self


class Claim(_Output):
    """A definite statement of the report."""

    statement: Text
    citations: Citations


class MissingEvidence(_Output):
    """Evidence the investigation did not obtain, and why."""

    what: Text
    reason: Text


class Suggestion(_Output):
    """A remediation for a person to confirm or reject."""

    action: Text
    command: Text | None = None
    risk: Risk
    citations: Citations


class Reference(_Output):
    """A runbook chunk the report refers to. Knowledge is not evidence."""

    knowledge_id: Text
    title: Text


class Report(_Output):
    """The structured conclusion of an investigation."""

    schema_version: SchemaVersion
    summary: Text
    impact: Text
    timeline: list[TimelineEntry]
    root_cause: RootCause
    claims: list[Claim]
    missing_evidence: list[MissingEvidence]
    suggestions: list[Suggestion]
    references: list[Reference]
    followup_questions: list[Text]


class AnswerSegment(_Output):
    """One paragraph of a follow-up answer."""

    text: Text
    citations: Annotated[
        list[Text], Field(description="IDs of the evidence or suggestions this paragraph rests on.")
    ]
    #: Models often leave the flag out when it is false. A paragraph without it still has
    #: to cite evidence (``citation_integrity``), so the default admits nothing uncited.
    insufficient_evidence: bool = Field(
        default=False,
        description="True when the evidence does not answer this part of the question.",
    )


class FollowupAnswer(_Output):
    """The answer to one follow-up question."""

    segments: Annotated[list[AnswerSegment], Field(min_length=1)]
    references: list[Text]


class PlanStep(_Output):
    """One step of an investigation plan."""

    goal: Text
    domain: Text
    suggested_tools: list[Text]
    expected_evidence: Text


class Plan(_Output):
    """An investigation plan: the first version, or the supplementary steps of a replan."""

    rationale: Text
    steps: Annotated[list[PlanStep], Field(min_length=1)]
    knowledge_refs: list[Text]


class Assessment(_Output):
    """Whether the evidence of a round suffices for a report."""

    sufficient: bool
    reason: Text
    hypothesis_category: RootCauseCategory | None
    missing: list[Text]


class Finding(_Output):
    """A short note of an evidence sub-agent on the evidence of its step. Not a conclusion."""

    statement: Text
    citations: Citations


class Findings(_Output):
    """All findings an evidence sub-agent submits for one step."""

    findings: list[Finding]


class OutputType(StrEnum):
    """The model output types that have a checklist."""

    REPORT = "report"
    FOLLOWUP_ANSWER = "followup_answer"
    PLAN = "plan"
    ASSESSMENT = "assessment"
    FINDINGS = "findings"


#: The schema of each output type.
SCHEMAS: dict[OutputType, type[_Output]] = {
    OutputType.REPORT: Report,
    OutputType.FOLLOWUP_ANSWER: FollowupAnswer,
    OutputType.PLAN: Plan,
    OutputType.ASSESSMENT: Assessment,
    OutputType.FINDINGS: Findings,
}

#: The JSON Schema dialect pydantic generates.
JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"


def json_schema(output_type: OutputType) -> dict[str, Any]:
    """Return the JSON Schema of one output type.

    The cross-field rule of the root cause (an identified root cause names a category
    and an object) is checked by pydantic only; the JSON Schema states every other rule.
    """
    return {"$schema": JSON_SCHEMA_DIALECT, **SCHEMAS[output_type].model_json_schema()}


def export_json_schemas() -> dict[str, dict[str, Any]]:
    """Return the JSON Schema of every output type, keyed by ``<type>.schema.json``."""
    return {f"{output_type}.schema.json": json_schema(output_type) for output_type in OutputType}
