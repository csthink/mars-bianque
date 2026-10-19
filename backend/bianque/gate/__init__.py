"""The evidence gate: deterministic checks every model output passes before it is shown.

``validate(output_type, output, context)`` is the only entry point; the runtime and the
evaluation both call it. The package depends on pydantic and the redaction engine only,
and never on the model gateway (an import-linter contract).
"""

from __future__ import annotations

from bianque.gate.checklists import CHECKLISTS, checklist, validate
from bianque.gate.context import FailedCall, GateContext
from bianque.gate.degrade import EvidenceItem, build_degraded_report
from bianque.gate.result import GateResult, Violation
from bianque.gate.schemas import (
    SCHEMA_VERSION,
    SCHEMAS,
    AnswerSegment,
    Assessment,
    Claim,
    Finding,
    Findings,
    FollowupAnswer,
    OutputType,
    Plan,
    PlanStep,
    Report,
    RootCause,
    export_json_schemas,
    json_schema,
)

__all__ = [
    "CHECKLISTS",
    "SCHEMAS",
    "SCHEMA_VERSION",
    "AnswerSegment",
    "Assessment",
    "Claim",
    "EvidenceItem",
    "FailedCall",
    "Finding",
    "Findings",
    "FollowupAnswer",
    "GateContext",
    "GateResult",
    "OutputType",
    "Plan",
    "PlanStep",
    "Report",
    "RootCause",
    "Violation",
    "build_degraded_report",
    "checklist",
    "export_json_schemas",
    "json_schema",
    "validate",
]
