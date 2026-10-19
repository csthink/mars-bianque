"""The checklist of each output type and the single validation entry point."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from bianque.gate.context import GateContext
from bianque.gate.result import GateResult, Violation
from bianque.gate.schemas import OutputType
from bianque.gate.validators import (
    Validator,
    check_schema,
    citation_integrity,
    forbidden_actions,
    knowledge_boundary,
    missing_evidence_consistency,
    redaction_residue,
    unknown_discipline,
)

#: The validators each output type goes through after ``schema``.
CHECKLISTS: Mapping[OutputType, tuple[Validator, ...]] = {
    OutputType.REPORT: (
        citation_integrity,
        knowledge_boundary,
        unknown_discipline,
        missing_evidence_consistency,
        forbidden_actions,
        redaction_residue,
    ),
    OutputType.FOLLOWUP_ANSWER: (
        citation_integrity,
        knowledge_boundary,
        forbidden_actions,
        redaction_residue,
    ),
    OutputType.PLAN: (knowledge_boundary, forbidden_actions, redaction_residue),
    OutputType.ASSESSMENT: (forbidden_actions, redaction_residue),
    OutputType.FINDINGS: (
        citation_integrity,
        knowledge_boundary,
        forbidden_actions,
        redaction_residue,
    ),
}


def checklist(output_type: OutputType) -> list[str]:
    """Return the validator names of one output type's checklist, ``schema`` first."""
    return ["schema", *(validator.__name__ for validator in CHECKLISTS[output_type])]


def validate(
    output_type: str, output: Mapping[str, Any] | BaseModel, context: GateContext
) -> GateResult:
    """Run the checklist of ``output_type`` on one model output.

    The runtime and the evaluation both call this function. An output type without a
    checklist is rejected. When the schema rejects the output, the other validators do
    not run, since the fields they check may be missing.

    Args:
        output_type: One of the ``OutputType`` values.
        output: The function call arguments the model returned, as a JSON object, or an
            already parsed schema model.
        context: The investigation data the output is checked against.

    Returns:
        The result; ``output`` holds the parsed model when every validator accepted it.
    """
    try:
        known_type = OutputType(output_type)
    except ValueError:
        return GateResult(
            str(output_type), (Violation("checklist", "(root)", "unregistered_type"),)
        )
    parsed, violations = check_schema(known_type, output)
    if parsed is None:
        return GateResult(known_type.value, tuple(sorted(violations)))
    data = parsed.model_dump(mode="json")
    for validator in CHECKLISTS[known_type]:
        violations += validator(known_type, data, context)
    if violations:
        return GateResult(known_type.value, tuple(sorted(set(violations))))
    return GateResult(known_type.value, (), parsed)
