"""The outcome of one validation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel


@dataclass(frozen=True, order=True)
class Violation:
    """One broken rule.

    A violation never repeats the text of the output or a detected secret: it is stored
    with the rejected output and sent back to the model as feedback for the retry.

    Attributes:
        validator: The validator name, such as ``citation_integrity``.
        path: The JSON path of the offending value, such as ``claims[2].citations[0]``.
        code: A fixed phrase naming the rule, such as ``unknown_evidence_id``.
    """

    validator: str
    path: str
    code: str

    def to_json(self) -> dict[str, str]:
        """Return the violation as a JSON object."""
        return {"validator": self.validator, "path": self.path, "code": self.code}


@dataclass(frozen=True)
class GateResult:
    """The result of validating one output.

    Attributes:
        output_type: The output type that was requested.
        violations: The broken rules, sorted; empty when the output passed.
        output: The parsed output when it passed, otherwise ``None``.
    """

    output_type: str
    violations: tuple[Violation, ...] = ()
    output: BaseModel | None = field(default=None, compare=False)

    @property
    def passed(self) -> bool:
        """Whether every validator of the checklist accepted the output."""
        return not self.violations

    def validators(self) -> list[str]:
        """Return the names of the validators that rejected the output, without duplicates.

        This is all an interface may show about a rejected output.
        """
        return sorted({violation.validator for violation in self.violations})

    def to_json(self) -> dict[str, Any]:
        """Return the result in the form stored in a ``gate_result`` column."""
        return {
            "output_type": self.output_type,
            "passed": self.passed,
            "violations": [violation.to_json() for violation in self.violations],
        }
